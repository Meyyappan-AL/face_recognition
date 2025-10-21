import os
import io
import threading
import sqlite3
import datetime
import json
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, url_for, session
from model import train_model_background, extract_embedding_for_image, MODEL_PATH
import hashlib
import cv2

# --- Global Configurations ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "attendance.db")
DATASET_DIR = os.path.join(APP_DIR, "dataset")
os.makedirs(DATASET_DIR, exist_ok=True)

TRAIN_STATUS_FILE = os.path.join(APP_DIR, "train_status.json")

# --- Camera & System Configuration ---
COOLDOWN_SECONDS = 60  # Time in seconds before the same student can be marked again
CCTV_URL = 0           # Use integer 0 for the default webcam index
cctv_running = False   # Global flag to control the monitoring thread
# ------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")

# --- Session and Security Configuration ---
app.secret_key = 'your_super_secret_key_change_this'
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=24)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ==============================================================================
# DATABASE HELPERS
# ==============================================================================
def init_db():
    """Initializes the database and creates tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Table for student information
    c.execute("""CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    roll TEXT, class TEXT, section TEXT, reg_no TEXT,
                    created_at TEXT
                )""")
    # Table for all attendance records
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, name TEXT, timestamp TEXT
                )""")
    # Table for user logins (admin, staff, student)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL, created_at TEXT
    )""")
    # Table for live notifications (IN/OUT actions)
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER, student_name TEXT, action TEXT, timestamp TEXT
            )""")
    conn.commit()
    conn.close()

init_db()

def create_default_users():
    """Creates default admin, staff, and student accounts if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()
    # Updated default users as per final plan
    users_to_create = [
        ('swathi', 'vcet@123', 'admin'),
        ('staff', 'staff123', 'staff'),
        ('student', 'student123', 'student')
    ]
    for username, password, role in users_to_create:
        c.execute('SELECT * FROM users WHERE username = ?', (username,))
        if not c.fetchone():
            hashed_password = hashlib.sha256(password.encode()).hexdigest()
            c.execute('INSERT INTO users (username, password, role, created_at) VALUES (?, ?, ?, ?)',
                      (username, hashed_password, role, now))
            print(f"Default user created - Username: {username}, Role: {role}")
    conn.commit()
    conn.close()

create_default_users()

# ==============================================================================
# AUTHENTICATION & ROLE DECORATORS
# ==============================================================================
def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or session.get('role') != 'student':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def staff_or_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or session.get('role') not in ['admin', 'staff']:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or session.get('role') != 'admin':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ==============================================================================
# MODEL TRAINING HELPERS
# ==============================================================================
def write_train_status(status_dict):
    with open(TRAIN_STATUS_FILE, "w") as f: json.dump(status_dict, f)

def read_train_status():
    if not os.path.exists(TRAIN_STATUS_FILE):
        return {"running": False, "progress": 0, "message": "Not trained"}
    try:
        with open(TRAIN_STATUS_FILE, "r") as f: return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {"running": False, "progress": 0, "message": "Status file corrupted."}

if not os.path.exists(TRAIN_STATUS_FILE):
    write_train_status({"running": False, "progress": 0, "message": "No training yet."})

# ==============================================================================
# REAL-TIME ATTENDANCE PROCESSING THREAD
# ==============================================================================
def process_frame_for_attendance(frame):
    """Processes a single video frame to detect and recognize a student."""
    from model import load_model_if_exists, predict_with_model
    try:
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret: return
        stream = io.BytesIO(buffer.tobytes())
        emb = extract_embedding_for_image(stream)
        if emb is None: return
        clf = load_model_if_exists()
        if clf is None: return
        pred_label, conf = predict_with_model(clf, emb)
        if conf < 0.7: return  # Increased confidence threshold for better accuracy
        student_id = int(pred_label)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(seconds=COOLDOWN_SECONDS)
        c.execute("SELECT id FROM attendance WHERE student_id=? AND timestamp >= ?", (student_id, cutoff_time.isoformat()))
        if c.fetchone():
            conn.close()
            return
        c.execute("SELECT name FROM students WHERE id=?", (student_id,))
        row = c.fetchone()
        name = row[0] if row else "Unknown"
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("INSERT INTO attendance (student_id, name, timestamp) VALUES (?, ?, ?)", (student_id, name, ts))
        c.execute("INSERT INTO notifications (student_id, student_name, action, timestamp) VALUES (?, ?, ?, ?)", (student_id, name, 'IN', ts))
        conn.commit()
        conn.close()
        print(f"ATTENDANCE MARKED: {name} (ID: {student_id}, Conf: {conf:.2f})")
    except Exception as e:
        print(f"Error processing frame: {e}")

def cctv_processing_thread_target():
    """The main loop for the background thread that captures and processes webcam frames."""
    global cctv_running
    cap = cv2.VideoCapture(CCTV_URL)
    if not cap.isOpened():
        print(f"ERROR: Cannot open video stream: {CCTV_URL}.")
        cctv_running = False
        return
    frame_skip = 10
    frame_count = 0
    print(f"Webcam Processing started. Reading every {frame_skip}th frame.")
    while cctv_running:
        ret, frame = cap.read()
        if not ret:
            print("Webcam stream failed. Stopping thread.")
            break
        frame_count += 1
        if frame_count % frame_skip == 0:
            process_frame_for_attendance(frame)
            frame_count = 0
    cap.release()
    print("Webcam Processing stopped.")
    cctv_running = False

# ==============================================================================
# ADMIN-ONLY API & CONTROL ROUTES
# ==============================================================================
@app.route("/cctv_control", methods=["POST"])
@admin_required
def cctv_control():
    """API for Admins to start/stop the live monitoring thread."""
    global cctv_running
    action = request.get_json().get('action')
    if action == 'start' and not cctv_running:
        if load_model_if_exists() is None:
            return jsonify({"status": "error", "message": "Model not trained. Please train the model first."}), 400
        cctv_running = True
        threading.Thread(target=cctv_processing_thread_target, daemon=True).start()
        return jsonify({"status": "started"})
    elif action == 'stop' and cctv_running:
        cctv_running = False
        return jsonify({"status": "stopping"})
    return jsonify({"status": "already_running" if cctv_running else "stopped"})

@app.route("/cctv_status", methods=["GET"])
@admin_required
def cctv_status():
    """API for Admins to poll the status of the monitoring thread."""
    global cctv_running
    return jsonify({"running": cctv_running, "camera_url": str(CCTV_URL)})
    
@app.route("/train_model", methods=["GET"])
@admin_required
def train_model_route():
    status = read_train_status()
    if status.get("running"): return jsonify({"status":"already_running"}), 202
    write_train_status({"running": True, "progress": 0, "message": "Starting training"})
    threading.Thread(target=train_model_background, args=(DATASET_DIR, lambda p,m: write_train_status({"running": True, "progress": p, "message": m})), daemon=True).start()
    return jsonify({"status":"started"}), 202

@app.route("/train_status", methods=["GET"])
@admin_required
def train_status():
    return jsonify(read_train_status())

@app.route("/students", methods=["GET"])
@admin_required
def students_list():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, roll, class, section, reg_no FROM students ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    data = [ {"id":r[0],"name":r[1],"roll":r[2],"class":r[3],"section":r[4],"reg_no":r[5]} for r in rows ]
    return jsonify({"students": data})

@app.route("/students/<int:sid>", methods=["DELETE"])
@admin_required
def delete_student(sid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM students WHERE id=?", (sid,))
    c.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
    c.execute("DELETE FROM notifications WHERE student_id=?", (sid,))
    conn.commit()
    conn.close()
    folder = os.path.join(DATASET_DIR, str(sid))
    if os.path.isdir(folder):
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    return jsonify({"deleted": True})

# ==============================================================================
# STAFF & ADMIN API ROUTES
# ==============================================================================
@app.route("/notifications", methods=["GET"])
@staff_or_admin_required
def get_notifications():
    """API for Staff/Admins to get the universal notification log."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT student_name, action, timestamp FROM notifications ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    data = [{"name": r[0], "action": r[1], "timestamp": datetime.datetime.fromisoformat(r[2]).strftime('%Y-%m-%d %H:%M:%S')} for r in rows]
    return jsonify({"notifications": data})

# ==============================================================================
# STUDENT-ONLY API ROUTES
# ==============================================================================
@app.route("/student_notifications", methods=["GET"])
@student_required
def get_student_notifications():
    """API for a student to get their own filtered notification log."""
    student_name = session.get('username') 
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM students WHERE name = ?", (student_name,))
    student_record = c.fetchone()
    if not student_record:
        conn.close()
        return jsonify({"notifications": [], "error": "Student record not found."}), 404
    student_db_id = student_record[0]
    c.execute("SELECT student_name, action, timestamp FROM notifications WHERE student_id = ? ORDER BY id DESC LIMIT 50", (student_db_id,))
    rows = c.fetchall()
    conn.close()
    data = [{"name": r[0], "action": r[1], "timestamp": datetime.datetime.fromisoformat(r[2]).strftime('%Y-%m-%d %H:%M:%S')} for r in rows]
    return jsonify({"notifications": data})

# ==============================================================================
# GENERAL (LOGGED-IN) API ROUTES
# ==============================================================================
@app.route("/sign_out_face", methods=["POST"])
@login_required
def sign_out_face():
    """API for any logged-in user to mark their own exit via facial recognition."""
    if "image" not in request.files: return jsonify({"status": "no_image"}), 400
    try:
        emb = extract_embedding_for_image(request.files["image"].stream)
        if emb is None: return jsonify({"status": "no_face"}), 200
        clf = load_model_if_exists()
        if clf is None: return jsonify({"status": "no_model"}), 200
        pred_label, conf = predict_with_model(clf, emb)
        if conf < 0.7: return jsonify({"status": "low_conf", "confidence": float(conf)}), 200
        student_id = int(pred_label)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name FROM students WHERE id=?", (student_id,))
        row = c.fetchone()
        name = row[0] if row else "Unknown"
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("INSERT INTO notifications (student_id, student_name, action, timestamp) VALUES (?, ?, ?, ?)", (student_id, name, 'OUT', ts))
        conn.commit()
        conn.close()
        return jsonify({"status": "signed_out", "name": name, "confidence": float(conf)}), 200
    except Exception as e:
        app.logger.exception("Sign out error")
        return jsonify({"status": "error", "error": str(e)}), 500

# ==============================================================================
# AUTHENTICATION & PAGE ROUTES
# ==============================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json()
        username, password = data.get('username'), data.get('password')
        hashed_password = hashlib.sha256(password.encode()).hexdigest()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT id, username, role FROM users WHERE username = ? AND password = ?', (username, hashed_password))
        user = c.fetchone()
        conn.close()
        if user:
            session.clear()
            session['logged_in'], session['user_id'], session['username'], session['role'] = True, user[0], user[1], user[2]
            session.permanent = True
            return jsonify({'success': True, 'role': user[2]})
        else:
            return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    
    if 'logged_in' in session:
        role = session.get('role')
        if role == 'admin': return redirect(url_for('index'))
        if role == 'staff': return redirect(url_for('staff_monitor'))
        if role == 'student': return redirect(url_for('student_dashboard'))
    
    return render_template('login.html')

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/")
@admin_required
def index():
    return render_template("index.html")

@app.route("/staff_monitor")
@staff_or_admin_required
def staff_monitor():
    return render_template("staff_monitor.html")

@app.route("/student_dashboard")
@student_required
def student_dashboard():
    return render_template("student_dashboard.html")

@app.route("/add_student", methods=["GET", "POST"])
@admin_required
def add_student():
    if request.method == "GET":
        return render_template("add_student.html")
    data = request.form
    name, roll, cls, sec, reg_no = data.get("name","").strip(), data.get("roll","").strip(), data.get("class","").strip(), data.get("sec","").strip(), data.get("reg_no","").strip()
    if not name: return jsonify({"error":"name required"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO students (name, roll, class, section, reg_no, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (name, roll, cls, sec, reg_no, datetime.datetime.utcnow().isoformat()))
    sid = c.lastrowid
    conn.commit()
    conn.close()
    os.makedirs(os.path.join(DATASET_DIR, str(sid)), exist_ok=True)
    return jsonify({"student_id": sid})

@app.route("/upload_face", methods=["POST"])
@admin_required
def upload_face():
    student_id = request.form.get("student_id")
    if not student_id: return jsonify({"error":"student_id required"}), 400
    files = request.files.getlist("images[]")
    saved = 0
    folder = os.path.join(DATASET_DIR, student_id)
    os.makedirs(folder, exist_ok=True)
    for f in files:
        try:
            path = os.path.join(folder, f"{datetime.datetime.utcnow().timestamp():.6f}_{saved}.jpg")
            f.save(path)
            saved += 1
        except Exception as e:
            app.logger.error("Save error: %s", e)
    return jsonify({"saved": saved})

@app.route("/attendance_record", methods=["GET"])
@staff_or_admin_required
def attendance_record():
    period = request.args.get("period", "all")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    q = "SELECT id, student_id, name, timestamp FROM attendance"
    params = ()
    if period == "daily":
        q += " WHERE date(timestamp) = date('now')"
    elif period == "weekly":
        q += " WHERE date(timestamp) >= date('now', '-7 days')"
    elif period == "monthly":
        q += " WHERE date(timestamp) >= date('now', '-30 days')"
    q += " ORDER BY timestamp DESC LIMIT 5000"
    c.execute(q, params)
    rows = c.fetchall()
    conn.close()
    return render_template("attendance_record.html", records=rows, period=period)

@app.route("/download_csv", methods=["GET"])
@staff_or_admin_required
def download_csv():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT student_id, name, timestamp FROM attendance ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()
    output = io.StringIO()
    output.write("Student ID,Name,Timestamp (UTC)\n")
    for r in rows:
        output.write(f'"{r[0]}","{r[1]}","{r[2]}"\n')
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="attendance.csv", mimetype="text/csv")

@app.route("/attendance_stats")
@staff_or_admin_required
def attendance_stats():
    import pandas as pd
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query("SELECT timestamp FROM attendance", conn)
    except pd.io.sql.DatabaseError:
        df = pd.DataFrame(columns=['timestamp'])
    conn.close()
    if df.empty:
        days = [(datetime.date.today() - datetime.timedelta(days=i)).strftime("%d-%b") for i in range(29, -1, -1)]
        return jsonify({"dates": days, "counts": [0]*30})
    df['date'] = pd.to_datetime(df['timestamp']).dt.date
    last_30 = [(datetime.date.today() - datetime.timedelta(days=i)) for i in range(29, -1, -1)]
    counts = [int(df[df['date'] == d].shape[0]) for d in last_30]
    dates = [d.strftime("%d-%b") for d in last_30]
    return jsonify({"dates": dates, "counts": counts})

# ==============================================================================
# APPLICATION RUN
# ==============================================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
