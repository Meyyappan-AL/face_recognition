import os
import io
import threading
import sqlite3
import datetime
import json
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, url_for, session, Response
from model import train_model_background, extract_embedding_for_image, load_model_if_exists, MODEL_PATH
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
CCTV_URL = 0           # Default: webcam index 0 (can be int index, file path, or RTSP URL)
current_source = 0     # Active source used by the monitoring thread
current_source_type = 'webcam'  # webcam | video | cctv
cctv_running = False   # Global flag to control the monitoring thread
cctv_stop_reason = ''  # Last stop reason/message
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)
ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.wmv'}
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
    c.execute("""CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    roll TEXT, class TEXT, section TEXT, reg_no TEXT,
                    created_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, name TEXT, timestamp TEXT, source TEXT DEFAULT 'auto', marked_by TEXT
                )""")
    for coldef in [("source", "TEXT DEFAULT 'auto'"), ("marked_by", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE attendance ADD COLUMN {coldef[0]} {coldef[1]}")
        except Exception:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL, student_id INTEGER, created_at TEXT
    )""")
    try:
        c.execute("ALTER TABLE users ADD COLUMN student_id INTEGER")
    except Exception:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER, student_name TEXT, action TEXT, timestamp TEXT, source TEXT DEFAULT 'auto', marked_by TEXT
            )""")
    for coldef in [("source", "TEXT DEFAULT 'auto'"), ("marked_by", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE notifications ADD COLUMN {coldef[0]} {coldef[1]}")
        except Exception:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS manual_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER, student_name TEXT, date TEXT, status TEXT, note TEXT,
                marked_by TEXT, marked_role TEXT, created_at TEXT
            )""")
    conn.commit()
    conn.close()


def _resolve_student_for_user():
    uid = session.get('user_id')
    username = (session.get('username') or '').strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT student_id FROM users WHERE id=?", (uid,))
        u = c.fetchone()
        if u and u[0]:
            c.execute("SELECT id,name,roll,class,section,reg_no,created_at FROM students WHERE id=?", (u[0],))
            row = c.fetchone()
            if row:
                conn.close(); return row
    except Exception:
        pass
    for q, params in [
        ("SELECT id,name,roll,class,section,reg_no,created_at FROM students WHERE name=? LIMIT 1", (username,)),
        ("SELECT id,name,roll,class,section,reg_no,created_at FROM students WHERE lower(name)=lower(?) LIMIT 1", (username,)),
        ("SELECT id,name,roll,class,section,reg_no,created_at FROM students WHERE reg_no=? OR roll=? LIMIT 1", (username, username)),
    ]:
        try:
            c.execute(q, params); row = c.fetchone()
            if row: conn.close(); return row
        except Exception:
            pass
    conn.close(); return None

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
            if request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json or request.path in ('/cctv_control','/upload_video','/cctv_status'):
                return jsonify({"error": "Unauthorized"}), 403
            return "Access Denied: Staff or Admin privileges required.", 403
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
# Debug counters (reset conceptually per process; used for visibility)
_attendance_debug = {"frames": 0, "no_face": 0, "no_model": 0, "low_conf": 0, "cooldown": 0, "marked": 0, "last": ""}

def process_frame_for_attendance(frame):
    """Processes a single video frame to detect and recognize a student."""
    global _attendance_debug
    try:
        _attendance_debug["frames"] += 1
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret: return
        stream = io.BytesIO(buffer.tobytes())
        emb = extract_embedding_for_image(stream)
        if emb is None:
            _attendance_debug["no_face"] += 1
            if _attendance_debug["frames"] % 50 == 0:
                print(f"[Attendance] scanned={_attendance_debug['frames']} no_face={_attendance_debug['no_face']} low_conf={_attendance_debug['low_conf']} marked={_attendance_debug['marked']} last={_attendance_debug['last']}")
            return
        clf = load_model_if_exists()
        if clf is None:
            _attendance_debug["no_model"] += 1
            return
        pred_label, conf = predict_with_model(clf, emb)
        _attendance_debug["last"] = f"id={pred_label} conf={conf:.2f}"
        if conf < 0.7:
            _attendance_debug["low_conf"] += 1
            if _attendance_debug["frames"] % 20 == 0:
                print(f"[Attendance] low confidence: {_attendance_debug['last']} (need >= 0.70)")
            return
        student_id = int(pred_label)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(seconds=COOLDOWN_SECONDS)
        c.execute("SELECT id FROM attendance WHERE student_id=? AND timestamp >= ?", (student_id, cutoff_time.isoformat()))
        if c.fetchone():
            _attendance_debug["cooldown"] += 1
            conn.close()
            return
        c.execute("SELECT name FROM students WHERE id=?", (student_id,))
        row = c.fetchone()
        name = row[0] if row else "Unknown"
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("INSERT INTO attendance (student_id, name, timestamp, source) VALUES (?, ?, ?, ?)", (student_id, name, ts, 'auto'))
        c.execute("INSERT INTO notifications (student_id, student_name, action, timestamp, source) VALUES (?, ?, ?, ?, ?)", (student_id, name, 'IN', ts, 'auto'))
        conn.commit()
        conn.close()
        _attendance_debug["marked"] += 1
        print(f"ATTENDANCE MARKED: {name} (ID: {student_id}, Conf: {conf:.2f})")
    except Exception as e:
        print(f"Error processing frame: {e}")

def cctv_processing_thread_target():
    """The main loop for the background thread that captures and processes frames."""
    global cctv_running, cctv_stop_reason, current_source, current_source_type
    source = current_source
    source_type = current_source_type
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        msg = f"Cannot open source: {source}."
        print(f"ERROR: {msg}")
        cctv_stop_reason = msg
        cctv_running = False
        return
    frame_skip = 10
    frame_count = 0
    cctv_stop_reason = ""
    print(f"Processing started ({source_type}). Reading every {frame_skip}th frame. Source={source}")
    while cctv_running:
        ret, frame = cap.read()
        if not ret:
            if source_type == 'video':
                cctv_stop_reason = "Video processing completed."
                print(cctv_stop_reason)
            else:
                cctv_stop_reason = "Stream ended or cannot read frames."
                print(f"Source read failed. Stopping thread. ({source})")
            break
        frame_count += 1
        if frame_count % frame_skip == 0:
            try:
                h, w = frame.shape[:2]
                if w > 640:
                    scale = 640 / float(w)
                    frame = cv2.resize(frame, (640, int(h * scale)))
            except Exception:
                pass
            process_frame_for_attendance(frame)
            frame_count = 0
    cap.release()
    if cctv_running:
        cctv_running = False
    if not cctv_stop_reason:
        cctv_stop_reason = "Monitoring stopped."
    print("Processing stopped.")

# ==============================================================================
# ADMIN-ONLY API & CONTROL ROUTES
# ==============================================================================
@app.route("/upload_video", methods=["POST"])
@staff_or_admin_required
def upload_video():
    """Upload a recorded video file to be processed for attendance (common page; no CCTV setup now)."""
    if "video" not in request.files:
        return jsonify({"status": "error", "message": "No video file provided."}), 400
    file = request.files["video"]
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "No file selected."}), 400
    from werkzeug.utils import secure_filename
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"status": "error", "message": f"Unsupported format. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}"}), 400
    save_name = f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
    save_path = os.path.join(UPLOADS_DIR, save_name)
    try:
        file.save(save_path)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to save video: {e}"}), 500
    return jsonify({"status": "ok", "filename": save_name, "path": save_path})


@app.route("/cctv_control", methods=["POST"])
@staff_or_admin_required
def cctv_control():
    """Start/stop attendance marking (webcam, uploaded video, or CCTV/RTSP)."""
    global cctv_running, current_source, current_source_type, cctv_stop_reason
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    if action == 'start' and not cctv_running:
        try:
            if load_model_if_exists() is None:
                return jsonify({"status": "error", "message": "Model not trained. Ask admin to train the model."}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": f"Model check failed: {e}"}), 500
        source_type = (data.get('source_type') or 'webcam').lower()
        source = data.get('source', 0)
        if source_type == 'webcam':
            try:
                source = int(source)
            except Exception:
                source = 0
        elif source_type == 'video':
            source = str(source)
            if not os.path.isabs(source):
                source = os.path.join(UPLOADS_DIR, source)
            if not os.path.exists(source):
                return jsonify({"status": "error", "message": "Video file not found. Upload a video first."}), 400
        elif source_type == 'cctv':
            source = str(source).strip()
            if not source:
                return jsonify({"status": "error", "message": "CCTV/RTSP URL is required."}), 400
        else:
            return jsonify({"status": "error", "message": "Invalid source_type."}), 400
        current_source = source
        current_source_type = source_type
        cctv_stop_reason = ""
        cctv_running = True
        threading.Thread(target=cctv_processing_thread_target, daemon=True).start()
        return jsonify({"status": "started", "source_type": source_type})
    elif action == 'stop' and cctv_running:
        cctv_running = False
        cctv_stop_reason = f"Stopped by {session.get('role','user')}."
        return jsonify({"status": "stopping"})
    return jsonify({"status": "already_running" if cctv_running else "stopped", "message": cctv_stop_reason})


@app.route("/cctv_status", methods=["GET"])
@staff_or_admin_required
def cctv_status():
    """Poll the status of the monitoring thread."""
    global cctv_running, current_source, current_source_type, cctv_stop_reason
    display_source = os.path.basename(str(current_source)) if current_source_type == 'video' else str(current_source)
    return jsonify({
        "running": cctv_running,
        "camera_url": str(current_source),
        "source_type": current_source_type,
        "source": display_source,
        "message": cctv_stop_reason
    })
    
@app.route("/train_model", methods=["GET"])
@admin_required
def train_model_route():
    status = read_train_status()
    if status.get("running") and status.get("progress", 0) < 100:
        return jsonify({"status":"already_running"}), 202
    write_train_status({"running": True, "progress": 0, "message": "Starting training"})
    threading.Thread(target=train_model_background, args=(DATASET_DIR, lambda p,m: write_train_status({"running": True, "progress": p, "message": m})), daemon=True).start()
    return jsonify({"status":"started"}), 202

@app.route("/train_status", methods=["GET"])
@admin_required
def train_status():
    return jsonify(read_train_status())

@app.route("/students", methods=["GET"])
@staff_or_admin_required
def students_list():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, roll, class, section, reg_no FROM students ORDER BY name ASC")
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


@app.route("/admin/students")
@admin_required
def admin_students_page():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, roll, class, section, reg_no, created_at FROM students ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    students = []
    for r in rows:
        folder = os.path.join(DATASET_DIR, str(r[0]))
        img_count = 0
        if os.path.isdir(folder):
            img_count = len([f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        students.append({"id": r[0], "name": r[1], "roll": r[2], "class": r[3], "section": r[4], "reg_no": r[5], "created_at": r[6], "image_count": img_count})
    return render_template("students_list.html", students=students)


@app.route("/student_photo/<int:sid>")
@admin_required
def student_photo(sid):
    folder = os.path.join(DATASET_DIR, str(sid))
    if not os.path.isdir(folder):
        abort(404)
    files = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not files:
        abort(404)
    files.sort()
    path = os.path.join(folder, files[-1])  # latest by name
    try:
        return send_file(path, mimetype="image/jpeg")
    except Exception:
        abort(404)

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
    student = _resolve_student_for_user()
    if not student:
        return jsonify({"notifications": [], "error": "No linked student profile found. Ask admin to add your student record (matching your username/name/reg no)."}), 404
    student_db_id = student[0]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT student_name, action, timestamp FROM notifications WHERE student_id = ? ORDER BY id DESC LIMIT 100", (student_db_id,))
    rows = c.fetchall()
    conn.close()
    data = [{"name": r[0], "action": r[1], "timestamp": datetime.datetime.fromisoformat(r[2]).strftime('%Y-%m-%d %H:%M:%S')} for r in rows]
    return jsonify({"notifications": data, "student": {"id": student[0], "name": student[1], "roll": student[2], "class": student[3], "section": student[4], "reg_no": student[5]}})


@app.route("/student_summary", methods=["GET"])
@student_required
def student_summary():
    student = _resolve_student_for_user()
    if not student:
        return jsonify({"ok": False, "error": "No linked student profile found."}), 404
    sid = student[0]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT action, timestamp FROM notifications WHERE student_id=? ORDER BY timestamp ASC", (sid,))
    events = c.fetchall()
    c.execute("SELECT count(*) FROM attendance WHERE student_id=?", (sid,))
    total_marks = c.fetchone()[0]
    conn.close()
    first_in = last_out = None
    present_days = set()
    for action, ts in events:
        day = (ts or "")[:10]
        if action == 'IN':
            present_days.add(day)
            if not first_in or ts < first_in: first_in = ts
        elif action == 'OUT':
            if not last_out or ts > last_out: last_out = ts
    def fmt(ts):
        if not ts: return None
        try: return datetime.datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:%M:%S')
        except Exception: return ts
    return jsonify({"ok": True, "student": {"id": student[0], "name": student[1], "roll": student[2], "class": student[3], "section": student[4], "reg_no": student[5]}, "total_attendance_marks": total_marks, "days_present": len(present_days), "first_in": fmt(first_in), "last_out": fmt(last_out)})

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
        c.execute("INSERT INTO notifications (student_id, student_name, action, timestamp, source) VALUES (?, ?, ?, ?, ?)", (student_id, name, 'OUT', ts, 'face'))
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

@app.route("/mark_attendance")
@staff_or_admin_required
def mark_attendance_page():
    """Common temporary page (no CCTV setup now): laptop camera or uploaded video for attendance marking."""
    if session.get('role') not in ('admin', 'staff'):
        return redirect(url_for('student_dashboard'))
    return render_template("mark_attendance.html")

@app.route("/staff_monitor")
@staff_or_admin_required
def staff_monitor():
    return render_template("staff_monitor.html")

@app.route("/student_dashboard")
@student_required
def student_dashboard():
    student = _resolve_student_for_user()
    student_info = None
    if student:
        student_info = {"id": student[0], "name": student[1], "roll": student[2], "class": student[3], "section": student[4], "reg_no": student[5]}
    return render_template("student_dashboard.html", student_info=student_info)

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

def _period_where(period, ts_col="timestamp"):
    if period == "daily":
        return f" WHERE date({ts_col}) = date('now')"
    if period == "weekly":
        return f" WHERE date({ts_col}) >= date('now', '-7 days')"
    if period == "monthly":
        return f" WHERE date({ts_col}) >= date('now', '-30 days')"
    return ""


def _day_bounds_for_period(period):
    today = datetime.date.today()
    if period == "daily":
        return today.isoformat(), today.isoformat()
    if period == "weekly":
        return (today - datetime.timedelta(days=6)).isoformat(), today.isoformat()
    if period == "monthly":
        return (today - datetime.timedelta(days=29)).isoformat(), today.isoformat()
    return None, None


def _manual_overrides(start_date, end_date):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    q = "SELECT student_id, status, note, marked_by, marked_role, date FROM manual_attendance"
    params = []
    if start_date and end_date:
        q += " WHERE date BETWEEN ? AND ?"; params = [start_date, end_date]
    elif start_date:
        q += " WHERE date>=?"; params = [start_date]
    q += " ORDER BY date DESC, id DESC"
    try:
        c.execute(q, params); rows = c.fetchall()
    except Exception:
        rows = []
    conn.close()
    # latest per (student_id, date) but for range report we need "any absent override"/"any present override"? keep latest per day
    out = {}
    for sid, status, note, by, role, d in rows:
        key = (sid, d)
        if key not in out:
            out[key] = {"status": status, "note": note or "", "marked_by": by or "", "marked_role": role or "", "date": d}
    return out


def _entry_exit_rows(period):
    """Compute first IN / last OUT per student from notifications in the selected period, honoring manual overrides."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, roll, class, section, reg_no FROM students ORDER BY name")
    students = c.fetchall()
    where = _period_where(period)
    c.execute("SELECT student_id, student_name, action, timestamp FROM notifications" + where + " ORDER BY timestamp ASC")
    events = c.fetchall()
    conn.close()

    by_id = {}
    for sid, sname, roll, cls, sec, reg in students:
        by_id[sid] = {"id": sid, "name": sname, "roll": roll or "", "class": cls or "", "section": sec or "", "reg_no": reg or "", "first_in": None, "last_out": None, "status": "Absent", "manual": ""}
    for sid, sname, action, ts in events:
        try:
            sid_i = int(sid) if sid is not None else None
        except Exception:
            sid_i = None
        if sid_i is None or sid_i not in by_id:
            sid_i = next((k for k,v in by_id.items() if v["name"] == sname), None)
            if sid_i is None: continue
        rec = by_id[sid_i]
        if action == "IN":
            if rec["first_in"] is None or ts < rec["first_in"]: rec["first_in"] = ts
            rec["status"] = "Present"
        elif action == "OUT":
            if rec["last_out"] is None or ts > rec["last_out"]: rec["last_out"] = ts
            if rec["first_in"] is not None:
                rec["status"] = "Exited" if rec["last_out"] >= rec["first_in"] else "Present"

    start_d, end_d = _day_bounds_for_period(period)
    if start_d and end_d:
        overrides = _manual_overrides(start_d, end_d)
        for (sid, d), ov in overrides.items():
            if sid not in by_id: continue
            rec = by_id[sid]
            if ov["status"] == "Absent":
                # explicit absent for that day in the selected range
                if period == "daily":
                    rec["status"] = "Absent"; rec["first_in"] = None; rec["last_out"] = None
                rec["manual"] = f"Absent({ov['marked_by'] or 'manual'})"
            elif ov["status"] == "Present" and rec["status"] == "Absent":
                rec["status"] = "Present (Manual)"; rec["manual"] = f"Present({ov['marked_by'] or 'manual'})"

    rows = []
    for rec in by_id.values():
        duration = ""
        if rec["first_in"] and rec["last_out"]:
            try:
                t1 = datetime.datetime.fromisoformat(rec["first_in"]); t2 = datetime.datetime.fromisoformat(rec["last_out"])
                if t2 >= t1:
                    mins = int((t2 - t1).total_seconds() // 60); duration = f"{mins // 60}h {mins % 60}m"
            except Exception:
                duration = ""
        def fmt(ts):
            if not ts: return ""
            try: return datetime.datetime.fromisoformat(ts).strftime('%Y-%m-%d %H:%M:%S')
            except Exception: return ts
        rows.append({**rec, "first_in_fmt": fmt(rec["first_in"]), "last_out_fmt": fmt(rec["last_out"]), "duration": duration})
    present = sum(1 for r in rows if r["status"] not in ("Absent",))
    absent = sum(1 for r in rows if r["status"] == "Absent")
    return rows, {"total": len(rows), "present": present, "absent": absent}


@app.route("/attendance_record", methods=["GET"])
@staff_or_admin_required
def attendance_record():
    period = request.args.get("period", "all")
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id, student_id, name, timestamp FROM attendance" + _period_where(period) + " ORDER BY timestamp DESC LIMIT 5000")
    records = c.fetchall(); conn.close()
    report_rows, summary = _entry_exit_rows(period)
    return render_template("attendance_record.html", records=records, period=period, report_rows=report_rows, summary=summary)

@app.route("/download_csv", methods=["GET"])
@staff_or_admin_required
def download_csv():
    period = request.args.get("period", "all")
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT student_id, name, timestamp FROM attendance" + _period_where(period) + " ORDER BY timestamp DESC")
    rows = c.fetchall(); conn.close()
    output = io.StringIO(); output.write("Student ID,Name,Timestamp (UTC)\n")
    for r in rows: output.write(f'"{r[0]}","{r[1]}","{r[2]}"\n')
    mem = io.BytesIO(output.getvalue().encode("utf-8")); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f"attendance_{period}.csv", mimetype="text/csv")

@app.route("/download_entry_exit_csv", methods=["GET"])
@staff_or_admin_required
def download_entry_exit_csv():
    period = request.args.get("period", "daily")
    rows, _ = _entry_exit_rows(period)
    output = io.StringIO()
    output.write("Student ID,Name,Roll,Class,Section,Reg No,Status,First IN (UTC),Last OUT (UTC),Duration,Manual\n")
    for r in rows:
        output.write(f'"{r["id"]}","{r["name"]}","{r["roll"]}","{r["class"]}","{r["section"]}","{r["reg_no"]}","{r["status"]}","{r["first_in_fmt"]}","{r["last_out_fmt"]}","{r["duration"]}","{r.get("manual","")}"\n')
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f"entry_exit_{period}.csv", mimetype="text/csv")


@app.route("/manual_attendance", methods=["POST"])
@staff_or_admin_required
def manual_attendance():
    data = request.get_json(silent=True) or {}
    sid = data.get("student_id")
    status = (data.get("status") or "").strip().title()
    date_str = (data.get("date") or "").strip() or datetime.date.today().isoformat()
    note = (data.get("note") or "").strip()
    if status not in ("Present", "Absent"):
        return jsonify({"ok": False, "error": "status must be Present or Absent"}), 400
    try:
        sid = int(sid)
        datetime.date.fromisoformat(date_str)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid student_id/date"}), 400
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id,name FROM students WHERE id=?", (sid,))
    row = c.fetchone()
    if not row:
        conn.close(); return jsonify({"ok": False, "error": "Student not found"}), 404
    marked_by = session.get('username') or 'user'
    marked_role = session.get('role') or ''
    now = datetime.datetime.utcnow().isoformat()
    c.execute("INSERT INTO manual_attendance (student_id,student_name,date,status,note,marked_by,marked_role,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (sid, row[1], date_str, status, note, marked_by, marked_role, now))
    if status == "Present":
        ts = f"{date_str}T{datetime.datetime.utcnow().strftime('%H:%M:%S')}"
        c.execute("INSERT INTO attendance (student_id,name,timestamp,source,marked_by) VALUES (?,?,?,?,?)", (sid, row[1], ts, 'manual', marked_by))
        c.execute("INSERT INTO notifications (student_id,student_name,action,timestamp,source,marked_by) VALUES (?,?,?,?,?,?,?)", (sid, row[1], 'IN', ts, 'manual', marked_by))
    elif status == "Absent":
        # remove auto/manual IN/OUT notifications for that day? keep simple: insert an ABSENT marker notification for visibility
        ts = f"{date_str}T{datetime.datetime.utcnow().strftime('%H:%M:%S')}"
        c.execute("INSERT INTO notifications (student_id,student_name,action,timestamp,source,marked_by) VALUES (?,?,?,?,?,?,?)", (sid, row[1], 'ABSENT', ts, 'manual', marked_by))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "student_id": sid, "name": row[1], "status": status, "date": date_str})

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

