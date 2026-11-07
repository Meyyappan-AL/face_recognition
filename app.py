import os
import io
import threading
import sqlite3
import datetime
import json
import re
import secrets
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, url_for, session
from werkzeug.utils import secure_filename
from model import train_model_background, extract_embedding_for_image, load_model_if_exists, MODEL_PATH, predict_with_model
import hashlib
import cv2

# --- Global Configurations ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "attendance.db")
DATASET_DIR = os.path.join(APP_DIR, "dataset")
TIMETABLE_DIR = os.path.join(APP_DIR, "timetables")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
OD_PROOFS_DIR = os.path.join(UPLOADS_DIR, "od_proofs")
os.makedirs(DATASET_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OD_PROOFS_DIR, exist_ok=True)
os.makedirs(TIMETABLE_DIR, exist_ok=True)
TRAIN_STATUS_FILE = os.path.join(APP_DIR, "train_status.json")

COOLDOWN_SECONDS = 60
CCTV_URL = 0
current_source = 0
current_source_type = 'webcam'
cctv_running = False
cctv_stop_reason = 'Auto-marking disabled: use Staff Session draft->submit flow (Phase 3).'
ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.wmv'}
MAX_OD_PROOF_MB = 50
PROTOTYPE_MODE = True  # keep data non-permanent until explicitly finalized

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=24)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024

# ==============================================================================
# HELPERS
# ==============================================================================
def _db(): return sqlite3.connect(DB_PATH)
def _hash_pw(pw): return hashlib.sha256((pw or '').encode()).hexdigest()
def _norm(s): return re.sub(r'\s+', ' ', (s or '').strip())
def _upper(s): return _norm(s).upper()

def _normalize_dob(dob):
    dob = _norm(dob)
    if not dob: return ''
    for fmt in ('%Y-%m-%d','%d-%m-%Y','%d/%m/%Y','%Y/%m/%d','%Y%m%d'):
        try: return datetime.datetime.strptime(dob, fmt).strftime('%Y-%m-%d')
        except Exception: pass
    return dob

def _staff_password(display_name):
    # password rule: name only (no Mr/Ms/Dr/initials), ALL CAPS
    name = re.sub(r'^(dr\.?|mr\.?|ms\.?|mrs\.?|prof\.?)\s*', '', _norm(display_name), flags=re.I)
    parts = [p for p in re.split(r'\s+', name) if not re.fullmatch(r'[A-Za-z]\.?', p)]
    return ' '.join(parts).strip().upper()

def load_timetable(class_name):
    path = os.path.join(TIMETABLE_DIR, f"{class_name}.json")
    if not os.path.exists(path): return None
    with open(path, 'r', encoding='utf-8-sig') as f: return json.load(f)

def all_timetable_staff():
    staff = {}; advisors = {}
    for cls in ('24IIT5A','24IIT5B'):
        tt = load_timetable(cls)
        if not tt: continue
        adv = tt.get('class_advisor') or {}
        if adv.get('initials'):
            advisors[adv['initials']] = {'class': cls, **adv}
            staff.setdefault(adv['initials'], {'code': adv['initials'], 'name': adv.get('name', adv['initials'])})
        for code, name in (tt.get('staff_directory') or {}).items():
            staff.setdefault(code, {'code': code, 'name': name})
    return staff, advisors

HOD_STAFF_CODE = 'RMY'  # Dr. Mynavathi (RMY)

def write_train_status(status_dict):
    with open(TRAIN_STATUS_FILE, "w") as f: json.dump(status_dict, f)

def read_train_status():
    if not os.path.exists(TRAIN_STATUS_FILE): return {"running": False, "progress": 0, "message": "Not trained"}
    try:
        with open(TRAIN_STATUS_FILE, "r") as f: return json.load(f)
    except Exception: return {"running": False, "progress": 0, "message": "Status unavailable"}

# ==============================================================================
# DATABASE
# ==============================================================================
def _ensure_column(c, table, col, coldef):
    try: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
    except Exception: pass

def init_db():
    conn = _db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, roll TEXT, class TEXT, section TEXT, reg_no TEXT, dob TEXT, created_at TEXT
                )""")
    for col,coldef in [('dob','TEXT'),('reg_no','TEXT'),('active','INTEGER DEFAULT 1')]: _ensure_column(c,'students',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, name TEXT, timestamp TEXT, source TEXT DEFAULT 'auto', marked_by TEXT
                )""")
    for col,coldef in [('source',"TEXT DEFAULT 'auto'"),('marked_by','TEXT')]: _ensure_column(c,'attendance',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL, student_id INTEGER, created_at TEXT
                )""")
    for col,coldef in [('student_id','INTEGER'),('staff_code','TEXT'),('display_name','TEXT'),('is_hod','INTEGER DEFAULT 0'),('is_advisor','INTEGER DEFAULT 0'),('advisor_class','TEXT'),('dob','TEXT')]:
        _ensure_column(c,'users',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, student_name TEXT, action TEXT, timestamp TEXT, source TEXT DEFAULT 'auto', marked_by TEXT
                )""")
    for col,coldef in [('source',"TEXT DEFAULT 'auto'"),('marked_by','TEXT')]: _ensure_column(c,'notifications',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS manual_attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, student_name TEXT, date TEXT, status TEXT, note TEXT,
                    marked_by TEXT, marked_role TEXT, created_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS attendance_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT, date TEXT, period INTEGER, subject TEXT, subject_display TEXT,
                    staff_code TEXT, staff_name TEXT, status TEXT DEFAULT 'draft', source TEXT,
                    created_at TEXT, submitted_at TEXT, notes TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS attendance_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER, student_id INTEGER, reg_no TEXT, student_name TEXT,
                    status TEXT, confidence REAL, flagged INTEGER DEFAULT 0, source TEXT,
                    marked_by TEXT, updated_at TEXT,
                    UNIQUE(session_id, student_id)
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS od_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, reg_no TEXT, student_name TEXT, class_name TEXT,
                    from_date TEXT, to_date TEXT, periods_json TEXT, day_wise INTEGER DEFAULT 0,
                    reason TEXT, proof_path TEXT, proof_name TEXT,
                    status TEXT DEFAULT 'pending',
                    advisor_code TEXT, advisor_decision TEXT, advisor_note TEXT, advisor_at TEXT,
                    hod_code TEXT, hod_decision TEXT, hod_note TEXT, hod_at TEXT,
                    created_at TEXT, updated_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS od_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER, student_id INTEGER, reg_no TEXT, student_name TEXT, class_name TEXT,
                    date TEXT, period INTEGER, subject TEXT, status TEXT DEFAULT 'OD',
                    issued_by TEXT, issued_role TEXT, created_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hod_staff_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hod_code TEXT, staff_code TEXT, permission TEXT, active INTEGER DEFAULT 1,
                    granted_at TEXT, revoked_at TEXT
                )""")
    conn.commit(); conn.close()
    if not os.path.exists(TRAIN_STATUS_FILE): write_train_status({"running": False, "progress": 0, "message": "No training yet."})

def seed_defaults():
    staff_map, advisors = all_timetable_staff()
    conn = _db(); c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    c.execute('SELECT id FROM users WHERE username=?', ('ADMIN',))
    if not c.fetchone():
        c.execute('INSERT INTO users (username,password,role,display_name,is_hod,is_advisor,created_at) VALUES (?,?,?,?,?,?,?)',
                  ('ADMIN', _hash_pw('ADMIN123'), 'admin', 'Administrator', 0, 0, now))
    else:
        c.execute('UPDATE users SET password=?, role=? WHERE username=?', (_hash_pw('ADMIN123'), 'admin', 'ADMIN'))
    for code, info in staff_map.items():
        display = _norm(info.get('name') or code)
        username = code.upper(); is_hod = 1 if code == HOD_STAFF_CODE else 0; adv = advisors.get(code)
        pw = _staff_password(display)
        c.execute('SELECT id FROM users WHERE username=?', (username,))
        if not c.fetchone():
            c.execute('INSERT INTO users (username,password,role,staff_code,display_name,is_hod,is_advisor,advisor_class,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (username, _hash_pw(pw), 'staff', code, display, is_hod, 1 if adv else 0, (adv or {}).get('class'), now))
        else:
            c.execute('UPDATE users SET password=?,staff_code=?,display_name=?,is_hod=?,is_advisor=?,advisor_class=?,role=? WHERE username=? AND role IN ("staff","hod","admin")',
                      (_hash_pw(pw), code, display, is_hod, 1 if adv else 0, (adv or {}).get('class'), 'staff', username))
    # remove legacy defaults
    c.execute("DELETE FROM users WHERE username IN ('swathi','staff','student')")
    conn.commit(); conn.close()

init_db(); seed_defaults()

# ==============================================================================
# AUTH / DECORATORS
# ==============================================================================
def login_required(f):
    @wraps(f)
    def w(*a,**k):
        if 'logged_in' not in session: return redirect(url_for('login'))
        return f(*a,**k)
    return w

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a,**k):
            if 'logged_in' not in session: return redirect(url_for('login'))
            if session.get('role') not in roles:
                if request.path.startswith('/api/') or request.is_json or request.headers.get('X-Requested-With')=='XMLHttpRequest':
                    return jsonify({'error':'forbidden'}), 403
                return "Access denied.", 403
            return f(*a,**k)
        return w
    return deco

admin_required = role_required('admin')
staff_required = role_required('staff','hod','admin')
student_required = role_required('student')
staff_or_admin_required = staff_required

def _current_user():
    return {'id':session.get('user_id'),'username':session.get('username'),'role':session.get('role'),
            'display_name':session.get('display_name'),'staff_code':session.get('staff_code'),
            'is_hod':bool(session.get('is_hod')), 'is_advisor':bool(session.get('is_advisor')),
            'advisor_class':session.get('advisor_class'), 'student_id':session.get('student_id')}

# ==============================================================================
# STUDENT RESOLUTION
# ==============================================================================
def _resolve_student_for_user():
    uid = session.get('user_id'); sid = session.get('student_id'); username = (session.get('username') or '').strip()
    conn = _db(); c = conn.cursor()
    try:
        if sid:
            c.execute("SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students WHERE id=?", (sid,))
            row = c.fetchone()
            if row: conn.close(); return row
        if uid:
            c.execute("SELECT student_id FROM users WHERE id=?", (uid,))
            u = c.fetchone()
            if u and u[0]:
                c.execute("SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students WHERE id=?", (u[0],))
                row = c.fetchone()
                if row: conn.close(); return row
    except Exception: pass
    for q, params in [
        ("SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students WHERE reg_no=? LIMIT 1", (username,)),
        ("SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students WHERE upper(reg_no)=upper(?) LIMIT 1", (username,)),
        ("SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students WHERE name=? LIMIT 1", (username,)),
    ]:
        try:
            c.execute(q, params); row = c.fetchone()
            if row: conn.close(); return row
        except Exception: pass
    conn.close(); return None

# ==============================================================================
# AUTH ROUTES
# ==============================================================================
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        role_tab = (data.get('role_tab') or '').lower()
        conn = _db(); c = conn.cursor(); user = None; login_error = 'Invalid credentials.'
        try:
            if role_tab == 'student':
                reg_no = _norm(data.get('reg_no') or data.get('username'))
                dob = _normalize_dob(data.get('dob') or data.get('password'))
                if not reg_no or not dob:
                    return jsonify({'success':False,'message':'Enter Register Number and Date of Birth.'}), 400
                c.execute('''SELECT u.id,u.username,u.role,u.student_id,u.display_name,u.staff_code,u.is_hod,u.is_advisor,u.advisor_class,
                                    s.id,s.dob,s.reg_no,s.name,s.class
                             FROM users u LEFT JOIN students s ON s.id=u.student_id
                             WHERE u.role="student" AND (upper(u.username)=upper(?) OR upper(s.reg_no)=upper(?))''', (reg_no, reg_no))
                row = c.fetchone()
                if row:
                    stored_dob = _normalize_dob(row[10] or '')
                    if stored_dob and stored_dob == dob:
                        user = row
                    else:
                        c.execute('SELECT password FROM users WHERE id=?', (row[0],)); pw=c.fetchone()
                        if pw and pw[0] == _hash_pw(dob): user = row
                if not user:
                    c.execute('SELECT id,name,reg_no,dob,class,section FROM students WHERE upper(reg_no)=upper(?)', (reg_no,))
                    s = c.fetchone()
                    if s and _normalize_dob(s[3]) == dob:
                        uname = _upper(s[2] or reg_no)
                        c.execute('SELECT id FROM users WHERE username=?', (uname,))
                        if not c.fetchone():
                            c.execute('INSERT INTO users (username,password,role,student_id,display_name,dob,created_at) VALUES (?,?,?,?,?,?,?)',
                                      (uname,_hash_pw(dob),'student',s[0],s[1],dob,datetime.datetime.utcnow().isoformat()))
                            conn.commit()
                        c.execute('''SELECT u.id,u.username,u.role,u.student_id,u.display_name,u.staff_code,u.is_hod,u.is_advisor,u.advisor_class,
                                            s.id,s.dob,s.reg_no,s.name,s.class
                                     FROM users u LEFT JOIN students s ON s.id=u.student_id WHERE u.username=?''', (uname,))
                        user = c.fetchone()
            else:
                user_id = _norm(data.get('user_id') or data.get('username'))
                password = data.get('password') or ''
                if not user_id or not password:
                    return jsonify({'success':False,'message':'Enter User ID and Password.'}), 400
                c.execute('''SELECT id,username,role,student_id,display_name,staff_code,is_hod,is_advisor,advisor_class,password
                             FROM users WHERE upper(username)=upper(?) OR upper(staff_code)=upper(?)''', (user_id, user_id))
                row = c.fetchone()
                if row and row[9] == _hash_pw(password):
                    if role_tab == 'admin' and row[2] != 'admin':
                        return jsonify({'success':False,'message':'This account is not an admin account.'}), 401
                    if role_tab == 'staff' and row[2] not in ('staff','hod','admin'):
                        return jsonify({'success':False,'message':'Use the correct login tab for this account.'}), 401
                    user = row[:9]
        finally:
            conn.close()
        if not user:
            return jsonify({'success':False,'message':login_error}), 401
        session.clear()
        if role_tab == 'student' or (len(user) >= 14 and user[2]=='student'):
            session.update({'logged_in':True,'user_id':user[0],'username':user[1],'role':'student','student_id':(user[9] if len(user)>9 else user[3]) or user[3],
                            'display_name':(user[12] if len(user)>12 else user[4]) or user[4] or user[1],'staff_code':None,'is_hod':False,'is_advisor':False,'advisor_class':(user[13] if len(user)>13 else None)})
            role_out='student'
        else:
            role_db = user[2]; role_out = 'hod' if (role_db in ('staff','hod') and user[6]) else role_db
            session.update({'logged_in':True,'user_id':user[0],'username':user[1],'role':role_db,'student_id':user[3],
                            'display_name':user[4] or user[1],'staff_code':user[5],'is_hod':bool(user[6]),'is_advisor':bool(user[7]),'advisor_class':user[8]})
        session.permanent = True
        return jsonify({'success':True,'role':role_out})
    if request.args.get('force') == '1': session.clear()
    if 'logged_in' in session:
        r = session.get('role')
        if r=='admin': return redirect(url_for('index'))
        if r in ('staff','hod'): return redirect(url_for('staff_monitor'))
        if r=='student': return redirect(url_for('student_dashboard'))
        session.clear()
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

# ==============================================================================
# PAGES
# ==============================================================================
@app.route('/')
@admin_required
def index(): return render_template('index.html', user=_current_user())

@app.route('/staff_monitor')
@staff_required
def staff_monitor(): return render_template('staff_monitor.html', user=_current_user())

@app.route('/student_dashboard')
@student_required
def student_dashboard():
    student = _resolve_student_for_user(); info=None
    if student:
        info = {"id":student[0],"name":student[1],"roll":student[2],"class":student[3],"section":student[4],"reg_no":student[5],"dob":student[7] if len(student)>7 else None}
    return render_template('student_dashboard.html', student_info=info, user=_current_user())

@app.route('/mark_attendance')
@staff_required
def mark_attendance_page():
    return render_template('mark_attendance.html', user=_current_user(), prototype_notice='Auto-marking/sign-out disabled. Staff Session draft→review→submit (period-wise) will replace this in Phase 3.')

# ==============================================================================
# STUDENTS / ADMIN
# ==============================================================================
@app.route('/add_student', methods=['GET','POST'])
@admin_required
def add_student():
    if request.method == 'GET': return render_template('add_student.html')
    data = request.form
    name = _norm(data.get('name')); roll=_norm(data.get('roll')); cls=_norm(data.get('class')); sec=_norm(data.get('sec') or data.get('section'))
    reg_no=_upper(data.get('reg_no')); dob=_normalize_dob(data.get('dob'))
    if not name: return jsonify({'error':'name required'}), 400
    if not reg_no or not dob: return jsonify({'error':'Register Number and DOB are required (student login = Reg No + DOB).'}), 400
    conn=_db(); c=conn.cursor()
    c.execute('INSERT INTO students (name,roll,class,section,reg_no,dob,created_at) VALUES (?,?,?,?,?,?,?)',
              (name,roll,cls,sec,reg_no,dob,datetime.datetime.utcnow().isoformat()))
    sid=c.lastrowid
    c.execute('SELECT id FROM users WHERE username=?',(reg_no,))
    if not c.fetchone():
        c.execute('INSERT INTO users (username,password,role,student_id,display_name,dob,created_at) VALUES (?,?,?,?,?,?,?)',
                  (reg_no,_hash_pw(dob),'student',sid,name,dob,datetime.datetime.utcnow().isoformat()))
    else:
        c.execute('UPDATE users SET student_id=?, display_name=?, dob=?, password=? WHERE username=? AND role="student"', (sid,name,dob,_hash_pw(dob),reg_no))
    conn.commit(); conn.close()
    os.makedirs(os.path.join(DATASET_DIR,str(sid)), exist_ok=True)
    return jsonify({'student_id':sid})

@app.route('/upload_face', methods=['POST'])
@admin_required
def upload_face():
    student_id = request.form.get('student_id')
    if not student_id: return jsonify({'error':'student_id required'}), 400
    files = request.files.getlist('images[]')
    saved=0; folder=os.path.join(DATASET_DIR,str(student_id)); os.makedirs(folder,exist_ok=True)
    for f in files:
        try:
            ext=os.path.splitext(secure_filename(f.filename or 'img.jpg'))[1].lower() or '.jpg'
            if ext not in ('.jpg','.jpeg','.png'): ext='.jpg'
            f.save(os.path.join(folder, f"{datetime.datetime.utcnow().timestamp():.6f}_{saved}{ext}")); saved+=1
        except Exception as e: app.logger.error('Save error: %s', e)
    return jsonify({'saved':saved})

@app.route('/students', methods=['GET'])
@staff_required
def students_list():
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,roll,class,section,reg_no,dob FROM students ORDER BY name ASC')
    rows=c.fetchall(); conn.close()
    return jsonify({'students':[{'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'dob':r[6]} for r in rows]})

@app.route('/admin/students')
@admin_required
def admin_students_page():
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,roll,class,section,reg_no,created_at,dob FROM students ORDER BY id DESC')
    rows=c.fetchall(); conn.close()
    students=[]
    for r in rows:
        folder=os.path.join(DATASET_DIR,str(r[0])); img_count=0
        if os.path.isdir(folder): img_count=len([f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png'))])
        students.append({'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'created_at':r[6],'dob':r[7],'image_count':img_count})
    return render_template('students_list.html', students=students)

@app.route('/student_photo/<int:sid>')
@admin_required
def student_photo(sid):
    folder=os.path.join(DATASET_DIR,str(sid))
    if not os.path.isdir(folder): abort(404)
    files=[f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png'))]
    if not files: abort(404)
    files.sort(); return send_file(os.path.join(folder,files[-1]), mimetype='image/jpeg')

@app.route('/students/<int:sid>', methods=['DELETE'])
@admin_required
def delete_student(sid):
    conn=_db(); c=conn.cursor()
    c.execute('DELETE FROM students WHERE id=?',(sid,))
    c.execute('DELETE FROM attendance WHERE student_id=?',(sid,))
    c.execute('DELETE FROM notifications WHERE student_id=?',(sid,))
    c.execute('DELETE FROM users WHERE student_id=? AND role="student"',(sid,))
    conn.commit(); conn.close()
    folder=os.path.join(DATASET_DIR,str(sid))
    if os.path.isdir(folder):
        import shutil; shutil.rmtree(folder, ignore_errors=True)
    return jsonify({'deleted':True})

# ==============================================================================
# TRAINING
# ==============================================================================
@app.route('/train_model', methods=['GET'])
@admin_required
def train_model_route():
    status=read_train_status()
    if status.get('running') and status.get('progress',0) < 100:
        return jsonify({'status':'already_running'}), 202
    write_train_status({'running':True,'progress':0,'message':'Starting training'})
    threading.Thread(target=train_model_background, args=(DATASET_DIR, lambda p,m: write_train_status({'running':True,'progress':p,'message':m})), daemon=True).start()
    return jsonify({'status':'started'}), 202

@app.route('/train_status', methods=['GET'])
@admin_required
def train_status(): return jsonify(read_train_status())

# ==============================================================================
# DISABLED legacy auto-mark/sign-out endpoints (ERP prototype safety)
# ==============================================================================
@app.route('/sign_out_face', methods=['POST'])
@login_required
def sign_out_face_disabled():
    return jsonify({'status':'disabled','message':'Sign-out by face is disabled. No exit is required; absence of exit is not marked absent. Attendance uses staff period session submission.'}), 410

@app.route('/cctv_control', methods=['POST'])
@staff_required
def cctv_control_disabled():
    global cctv_running, cctv_stop_reason
    cctv_running=False; cctv_stop_reason='Auto-marking disabled in ERP prototype. Use draft→submit session flow.'
    return jsonify({'status':'disabled','message':cctv_stop_reason}), 410

@app.route('/cctv_status', methods=['GET'])
@staff_required
def cctv_status_disabled():
    return jsonify({'running':False,'message':'Auto-marking disabled in ERP prototype. Use draft→submit session flow.','source_type':current_source_type,'source':str(current_source)})

@app.route('/upload_video', methods=['POST'])
@staff_required
def upload_video_legacy():
    if 'video' not in request.files: return jsonify({'status':'error','message':'No video file provided.'}), 400
    file=request.files['video']
    if not file or not file.filename: return jsonify({'status':'error','message':'No file selected.'}), 400
    filename=secure_filename(file.filename); ext=os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({'status':'error','message':f'Unsupported format. Allowed: {", ".join(sorted(ALLOWED_VIDEO_EXTENSIONS))}'}), 400
    save_name=f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}_{filename}"
    file.save(os.path.join(UPLOADS_DIR, save_name))
    return jsonify({'status':'ok','filename':save_name,'path':save_name,'message':'Uploaded. Session draft processing will be enabled in Phase 3.'})

# ==============================================================================
# REPORTS / STATS / COMPAT APIs
# ==============================================================================
def _period_where(period, ts_col='timestamp'):
    if period=='daily': return f" WHERE date({ts_col})=date('now')"
    if period=='weekly': return f" WHERE date({ts_col})>=date('now','-7 days')"
    if period=='monthly': return f" WHERE date({ts_col})>=date('now','-30 days')"
    return ''

@app.route('/attendance_record', methods=['GET'])
@staff_required
def attendance_record():
    period=request.args.get('period','all')
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,student_id,name,timestamp FROM attendance'+_period_where(period)+' ORDER BY timestamp DESC LIMIT 5000')
    records=c.fetchall(); conn.close()
    return render_template('attendance_record.html', records=records, period=period, report_rows=[], summary={'total':0,'present':0,'absent':0,'note':'Period-session reports populate from submitted sessions (Phase 3). Missing exit never marks absent.'})

@app.route('/download_csv', methods=['GET'])
@staff_required
def download_csv():
    period=request.args.get('period','all'); conn=_db(); c=conn.cursor()
    c.execute('SELECT student_id,name,timestamp FROM attendance'+_period_where(period)+' ORDER BY timestamp DESC')
    rows=c.fetchall(); conn.close()
    output=io.StringIO(); output.write('Student ID,Name,Timestamp (UTC)\n')
    for r in rows: output.write(f'"{r[0]}","{r[1]}","{r[2]}"\n')
    mem=io.BytesIO(output.getvalue().encode('utf-8')); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f'attendance_{period}.csv', mimetype='text/csv')

@app.route('/attendance_stats')
@staff_required
def attendance_stats():
    conn=_db(); c=conn.cursor()
    try: c.execute('SELECT timestamp FROM attendance'); rows=c.fetchall()
    except Exception: rows=[]
    conn.close()
    days=[(datetime.date.today()-datetime.timedelta(days=i)).strftime('%d-%b') for i in range(29,-1,-1)]
    counts=[0]*30
    for (ts,) in rows:
        try:
            d=datetime.datetime.fromisoformat(ts).date(); idx=29-(datetime.date.today()-d).days
            if 0<=idx<30: counts[idx]+=1
        except Exception: pass
    return jsonify({'dates':days,'counts':counts})

@app.route('/student_notifications', methods=['GET'])
@student_required
def get_student_notifications():
    student=_resolve_student_for_user()
    if not student: return jsonify({'notifications':[],'error':'No linked student profile found.'}), 404
    conn=_db(); c=conn.cursor(); c.execute('SELECT student_name,action,timestamp FROM notifications WHERE student_id=? ORDER BY id DESC LIMIT 100',(student[0],))
    rows=c.fetchall(); conn.close()
    data=[{'name':r[0],'action':r[1],'timestamp':datetime.datetime.fromisoformat(r[2]).strftime('%Y-%m-%d %H:%M:%S') if r[2] else ''} for r in rows]
    return jsonify({'notifications':data,'student':{'id':student[0],'name':student[1],'roll':student[2],'class':student[3],'section':student[4],'reg_no':student[5]}})

@app.route('/student_summary', methods=['GET'])
@student_required
def student_summary():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile found.'}), 404
    conn=_db(); c=conn.cursor(); c.execute('SELECT count(*) FROM attendance WHERE student_id=?',(student[0],)); total=c.fetchone()[0]; conn.close()
    return jsonify({'ok':True,'student':{'id':student[0],'name':student[1],'roll':student[2],'class':student[3],'section':student[4],'reg_no':student[5],'dob':student[7] if len(student)>7 else None},
                    'total_attendance_marks':total,'days_present':0,'last_out':None,'note':'Period-wise OD-aware summary after Phase 3/4. No exit is required and missing exit never marks absent.'})

@app.route('/notifications', methods=['GET'])
@staff_required
def get_notifications():
    conn=_db(); c=conn.cursor(); c.execute('SELECT student_name,action,timestamp FROM notifications ORDER BY id DESC LIMIT 20')
    rows=c.fetchall(); conn.close()
    data=[{'name':r[0],'action':r[1],'timestamp':datetime.datetime.fromisoformat(r[2]).strftime('%Y-%m-%d %H:%M:%S') if r[2] else ''} for r in rows]
    return jsonify({'notifications':data})

@app.route('/manual_attendance', methods=['POST'])
@staff_required
def manual_attendance():
    data=request.get_json(silent=True) or {}; sid=data.get('student_id'); status=(data.get('status') or '').strip().title(); date_str=(data.get('date') or '').strip() or datetime.date.today().isoformat()
    if status not in ('Present','Absent'): return jsonify({'ok':False,'error':'status must be Present or Absent'}), 400
    try: sid=int(sid); datetime.date.fromisoformat(date_str)
    except Exception: return jsonify({'ok':False,'error':'Invalid student_id/date'}), 400
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,name FROM students WHERE id=?',(sid,)); row=c.fetchone()
    if not row: conn.close(); return jsonify({'ok':False,'error':'Student not found'}), 404
    marked_by=session.get('display_name') or session.get('username') or 'user'; now=datetime.datetime.utcnow().isoformat()
    c.execute('INSERT INTO manual_attendance (student_id,student_name,date,status,note,marked_by,marked_role,created_at) VALUES (?,?,?,?,?,?,?,?)',
              (sid,row[1],date_str,status,(data.get('note') or '').strip(),marked_by,session.get('role') or '',now))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'student_id':sid,'name':row[1],'status':status,'date':date_str})

@app.route('/api/timetables')
@staff_required
def api_timetables():
    out={}
    for cls in ('24IIT5A','24IIT5B'): out[cls]=load_timetable(cls)
    return jsonify({'ok':True,'timetables':out})

@app.route('/api/me')
@login_required
def api_me(): return jsonify({'ok':True,'user':_current_user()})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
