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
PROTOTYPE_MODE = False  # data persistent (per user confirmation)

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
    return redirect(url_for('take_attendance_page'))

# ==============================================================================
# STUDENTS / ADMIN
# ==============================================================================
@app.route('/add_student', methods=['GET','POST'])
@admin_required
def add_student():
    if request.method == 'GET': return render_template('add_student.html', classes=['24IIT5A','24IIT5B'])
    data = request.form
    name = _norm(data.get('name')); roll=_norm(data.get('roll')); cls=_norm(data.get('class')); sec=_norm(data.get('sec') or data.get('section'))
    reg_no=_upper(data.get('reg_no')); dob=_normalize_dob(data.get('dob'))
    email=_norm(data.get('email')); phone=_norm(data.get('phone')); gender=_norm(data.get('gender')); blood=_norm(data.get('blood_group'))
    guardian_name=_norm(data.get('guardian_name')); guardian_phone=_norm(data.get('guardian_phone')); address=_norm(data.get('address'))
    if not name: return jsonify({'error':'name required'}), 400
    if not reg_no or not dob: return jsonify({'error':'Register Number and DOB are required (student login = Reg No + DOB).'}), 400
    conn=_db(); c=conn.cursor()
    for col in ['email','phone','gender','blood_group','guardian_name','guardian_phone','address']:
        _ensure_column(c,'students',col,'TEXT')
    c.execute('''INSERT INTO students (name,roll,class,section,reg_no,dob,email,phone,gender,blood_group,guardian_name,guardian_phone,address,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (name,roll,cls,sec,reg_no,dob,email,phone,gender,blood,guardian_name,guardian_phone,address,datetime.datetime.utcnow().isoformat()))
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
    for col in ['email','phone','guardian_name','guardian_phone','address','blood_group','gender']: _ensure_column(c,'students',col,'TEXT')
    c.execute('SELECT id,name,roll,class,section,reg_no,created_at,dob,email,phone,guardian_name,guardian_phone,active FROM students ORDER BY class,name,id DESC')
    rows=c.fetchall(); conn.close()
    students=[]
    for r in rows:
        folder=os.path.join(DATASET_DIR,str(r[0])); img_count=0
        if os.path.isdir(folder): img_count=len([f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png'))])
        students.append({'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'created_at':r[6],'dob':r[7],
                         'email':r[8] or '','phone':r[9] or '','guardian_name':r[10] or '','guardian_phone':r[11] or '',
                         'active':r[12] if len(r)>12 else 1,'image_count':img_count})
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


# ERP_MANUAL_ATTENDANCE
DAY_KEYS=['MON','TUE','WED','THU','FRI','SAT','SUN']

def _day_key(date_str):
    try: return DAY_KEYS[datetime.date.fromisoformat(date_str).weekday()]
    except Exception: return DAY_KEYS[datetime.datetime.utcnow().weekday()]

def timetable_cell(class_name, date_str, period):
    tt=load_timetable(class_name)
    if not tt: return None
    dk=_day_key(date_str)
    sched=(tt.get('schedule') or {}).get(dk) or []
    try: period=int(period)
    except Exception: return None
    for cell in sched:
        if int(cell.get('period') or 0)==period:
            slot=next((x for x in (tt.get('slots') or []) if int(x.get('period') or 0)==period), {})
            out=dict(cell); out['day']=dk
            out['time']=f"{slot.get('start','')}-{slot.get('end','')}" if slot else ''
            out['label']=slot.get('label',''); return out
    return None

def _is_assigned_staff(class_name, date_str, period):
    code=(session.get('staff_code') or session.get('username') or '').upper()
    cell=timetable_cell(class_name,date_str,period)
    if not cell or not code: return False
    staff=[(x or '').upper() for x in (cell.get('staff') or [])]
    return code in staff or bool(re.search(r'\b'+re.escape(code)+r'\b', (cell.get('display') or '').upper()))

def can_mark_period(class_name, date_str, period):
    role=session.get('role')
    if role=='admin': return True,'Admin (universal)'
    if session.get('is_hod'): return True,'HOD override'
    if session.get('is_advisor') and session.get('advisor_class')==class_name: return True,'Class advisor (own class, full day)'
    if _is_assigned_staff(class_name,date_str,period): return True,'Assigned/handling staff'
    cell=timetable_cell(class_name,date_str,period)
    disp=(cell or {}).get('display','')
    return False,f"Not assigned: {disp} (only listed/handling staff for this period; advisor own-class full day; HOD/admin override)."

def _student_photo_path(sid):
    folder=os.path.join(DATASET_DIR,str(sid))
    if not os.path.isdir(folder): return None
    files=[f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png'))]
    if not files: return None
    files.sort(); return os.path.join(folder,files[-1])

@app.route('/take_attendance')
@staff_required
def take_attendance_page():
    return render_template('take_attendance.html', user=_current_user(), classes=['24IIT5A','24IIT5B'], today=datetime.date.today().isoformat())

@app.route('/profile')
@login_required
def profile_page():
    student=_resolve_student_for_user() if session.get('role')=='student' else None
    staff=None
    if session.get('role')!='student':
        conn=_db(); c=conn.cursor(); c.execute('SELECT username,display_name,staff_code,role,is_hod,is_advisor,advisor_class,phone,email FROM users WHERE id=?',(session.get('user_id'),))
        r=c.fetchone(); conn.close()
        if r: staff={'username':r[0],'display_name':r[1],'staff_code':r[2],'role':r[3],'is_hod':r[4],'is_advisor':r[5],'advisor_class':r[6],'phone':r[7] if len(r)>7 else '','email':r[8] if len(r)>8 else ''}
    return render_template('profile.html', user=_current_user(), student=student, staff=staff)

@app.route('/api/profile/save', methods=['POST'])
@login_required
def api_profile_save():
    data=request.get_json(silent=True) or {}
    conn=_db(); c=conn.cursor(); uid=session.get('user_id'); role=session.get('role')
    try:
        if role=='student':
            student=_resolve_student_for_user()
            if not student: return jsonify({'ok':False,'error':'Student profile not linked'}), 404
            for col in ['phone','email','address','guardian_name','guardian_phone','blood_group','gender']:
                _ensure_column(c,'students',col,'TEXT')
            c.execute('UPDATE students SET phone=?,email=?,address=?,guardian_name=?,guardian_phone=?,blood_group=?,gender=? WHERE id=?',
                      ((data.get('phone') or '').strip(),(data.get('email') or '').strip(),(data.get('address') or '').strip(),
                       (data.get('guardian_name') or '').strip(),(data.get('guardian_phone') or '').strip(),
                       (data.get('blood_group') or '').strip(),(data.get('gender') or '').strip(),student[0]))
        else:
            for col in ['phone','email']: _ensure_column(c,'users',col,'TEXT')
            c.execute('UPDATE users SET phone=?,email=? WHERE id=?',((data.get('phone') or '').strip(),(data.get('email') or '').strip(),uid))
        newpw=(data.get('new_password') or '').strip()
        if newpw:
            if len(newpw)<4: conn.close(); return jsonify({'ok':False,'error':'Password too short (min 4)'}), 400
            c.execute('UPDATE users SET password=? WHERE id=?',(_hash_pw(newpw),uid))
        conn.commit(); return jsonify({'ok':True})
    finally:
        try: conn.close()
        except Exception: pass

@app.route('/student_photo_safe/<int:sid>')
@staff_required
def student_photo_safe(sid):
    p=_student_photo_path(sid)
    if not p: abort(404)
    return send_file(p, mimetype='image/jpeg')

@app.route('/api/timetable/cell')
@staff_required
def api_timetable_cell():
    cls=(request.args.get('class') or '').strip(); date=request.args.get('date') or datetime.date.today().isoformat(); period=request.args.get('period')
    cell=timetable_cell(cls,date,period); ok,reason=can_mark_period(cls,date,period)
    return jsonify({'ok':bool(cell),'class':cls,'date':date,'period':period,'day':_day_key(date),'cell':cell,'can_mark':ok,'access_reason':reason})

@app.route('/api/attendance/roster')
@staff_required
def api_attendance_roster():
    cls=(request.args.get('class') or '').strip(); date=request.args.get('date') or datetime.date.today().isoformat(); period=request.args.get('period')
    ok,reason=can_mark_period(cls,date,period)
    if not ok: return jsonify({'ok':False,'error':reason}), 403
    try: period=int(period)
    except Exception: return jsonify({'ok':False,'error':'Invalid period'}), 400
    cell=timetable_cell(cls,date,period) or {}
    disp=(cell.get('display') or cell.get('subject') or '').strip()
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,date,period,disp))
    sr=c.fetchone(); sess_id=sr[0] if sr else None
    existing={}
    if sess_id:
        c.execute('SELECT student_id,status FROM attendance_entries WHERE session_id=?',(sess_id,))
        for r in c.fetchall(): existing[int(r[0])]=(r[1] or 'Absent')
    c.execute('SELECT id,name,roll,class,section,reg_no,dob,phone,email,guardian_name,guardian_phone FROM students WHERE upper(class)=upper(?) AND (active IS NULL OR active=1) ORDER BY roll, reg_no, name',(cls,))
    rows=c.fetchall(); conn.close()
    is_subject=_is_assigned_staff(cls,date,period)
    students=[]
    for r in rows:
        st=existing.get(int(r[0]), 'Absent')
        locked_absent = (st=='Absent') and (not is_subject) and (session.get('role')!='admin')
        students.append({'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'dob':r[6],
                         'phone':(r[7] if len(r)>7 else '') or '','email':(r[8] if len(r)>8 else '') or '',
                         'guardian_name':(r[9] if len(r)>9 else '') or '','guardian_phone':(r[10] if len(r)>10 else '') or '',
                         'photo_url':f'/student_photo_safe/{r[0]}' if _student_photo_path(r[0]) else None,
                         'default_status':'Absent','status':st,'locked_absent':locked_absent,'session_id':sess_id})
    return jsonify({'ok':True,'class':cls,'date':date,'period':int(period),'day':_day_key(date),'cell':cell,'students':students,
                    'session_id':sess_id,'is_subject_staff':is_subject,
                    'note':'Default Absent. If already marked Absent, only subject/handling staff (or Admin) can change it. Period OD may be assigned by assigned/consent staff for handled periods; day OD requires Advisor approval then HOD approval.'})

@app.route('/api/attendance/submit', methods=['POST'])
@staff_required
def api_attendance_submit():
    data=request.get_json(silent=True) or {}
    cls=(data.get('class') or '').strip(); date=(data.get('date') or '').strip(); period=data.get('period'); marks=data.get('marks') or []
    ok,reason=can_mark_period(cls,date,period)
    if not ok: return jsonify({'ok':False,'error':reason}), 403
    try: datetime.date.fromisoformat(date); period=int(period)
    except Exception: return jsonify({'ok':False,'error':'Invalid date/period'}), 400
    cell=timetable_cell(cls,date,period) or {}
    disp=(cell.get('display') or (data.get('subject') or '')).strip(); subj=(cell.get('subject') or disp).strip()
    marked_by=session.get('display_name') or session.get('username') or 'staff'; now=datetime.datetime.utcnow().isoformat()
    is_subject=_is_assigned_staff(cls,date,period); is_admin=(session.get('role')=='admin')
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,date,period,disp))
    row=c.fetchone()
    if row:
        sess=row[0]; c.execute('UPDATE attendance_sessions SET subject=?,subject_display=?,staff_code=?,staff_name=?,status=?,source=?,submitted_at=?,notes=? WHERE id=?',
                               (subj,disp,session.get('staff_code'),marked_by,'submitted','manual',now,(data.get('notes') or ''),sess))
    else:
        c.execute('INSERT INTO attendance_sessions (class_name,date,period,subject,subject_display,staff_code,staff_name,status,source,created_at,submitted_at,notes) VALUES (?,?,?,?,?,?,?, "submitted","manual",?,?,?)',
                  (cls,date,period,subj,disp,session.get('staff_code'),marked_by,now,now,(data.get('notes') or '')))
        sess=c.lastrowid
    saved=0; blocked=0; details=[]
    for m in marks:
        try:
            stid=int(m.get('student_id')); status=(m.get('status') or 'Absent').strip().upper()
            status={'PRESENT':'Present','ABSENT':'Absent','OD':'OD','LATE':'Late'}.get(status,'Absent')
            c.execute('SELECT status FROM attendance_entries WHERE session_id=? AND student_id=?',(sess,stid)); prev=c.fetchone(); prev_st=(prev[0] if prev else None)
            if prev_st=='Absent' and status!='Absent' and not (is_subject or is_admin):
                blocked += 1; details.append({'student_id':stid,'blocked':'Previously Absent: only subject/handling staff or Admin can change'}); continue
            # Period OD allowed for assigned/handling staff (and advisor/HOD/admin context if they are marking this period) - stored as staff-issued period OD
            c.execute('SELECT reg_no,name FROM students WHERE id=?',(stid,)); sr=c.fetchone(); reg=sr[0] if sr else ''; sname=sr[1] if sr else (m.get('name') or '')
            c.execute("INSERT INTO attendance_entries (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at) VALUES (?,?,?,?,?,?,0,'manual',?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET reg_no=excluded.reg_no,student_name=excluded.student_name,status=excluded.status,source=excluded.source,marked_by=excluded.marked_by,updated_at=excluded.updated_at",
                      (sess,stid,reg,sname,status,None,marked_by,now))
            if status=='OD':
                # record period OD issued by handling staff (no proof required for handling staff)
                c.execute('INSERT INTO od_records (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at) VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)',
                          (stid,reg,sname,cls,date,period,disp,'OD',marked_by,session.get('role') or 'staff',now))
            saved+=1
        except Exception: pass
    conn.commit(); conn.close()
    return jsonify({'ok':True,'session_id':sess,'saved':saved,'blocked':blocked,'details':details,'class':cls,'date':date,'period':period,'subject':disp})



# ERP_OD_WORKFLOW
def _save_od_proof(file_storage):
    if not file_storage or not file_storage.filename: return None,None
    fname=secure_filename(file_storage.filename)
    ext=os.path.splitext(fname)[1].lower()
    save=f"od_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext or '.bin'}"
    path=os.path.join(OD_PROOFS_DIR, save)
    file_storage.save(path)
    return path, fname

@app.route('/od')
@login_required
def od_page():
    role=session.get('role'); student=_resolve_student_for_user() if role=='student' else None
    return render_template('od.html', user=_current_user(), student=student, classes=['24IIT5A','24IIT5B'], today=datetime.date.today().isoformat())

@app.route('/api/od/request', methods=['POST'])
@student_required
def api_od_request():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'Student profile not linked'}), 404
    reason=(request.form.get('reason') or '').strip()
    from_date=(request.form.get('from_date') or '').strip(); to_date=(request.form.get('to_date') or from_date).strip()
    day_wise = 1 if (request.form.get('day_wise') in ('1','true','on','yes')) else 0
    periods_json=(request.form.get('periods_json') or '').strip() or None
    if not reason or not from_date: return jsonify({'ok':False,'error':'Reason and date are required'}), 400
    proof_path, proof_name = _save_od_proof(request.files.get('proof'))
    if day_wise and not proof_path:
        return jsonify({'ok':False,'error':'Day OD requires proof/letter upload (max 50MB).'}), 400
    now=datetime.datetime.utcnow().isoformat()
    conn=_db(); c=conn.cursor()
    c.execute("INSERT INTO od_requests (student_id,reg_no,student_name,class_name,from_date,to_date,periods_json,day_wise,reason,proof_path,proof_name,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending_advisor',?,?)",
              (student[0], student[5], student[1], student[3], from_date, to_date, periods_json, day_wise, reason, proof_path, proof_name, now, now))
    rid=c.lastrowid; conn.commit(); conn.close()
    return jsonify({'ok':True,'request_id':rid,'status':'pending_advisor','message':'OD request submitted. Day OD: Advisor must approve, then HOD must approve. If rejected by advisor/HOD, OD is cancelled.'})

@app.route('/api/od/my')
@student_required
def api_od_my():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':True,'requests':[]})
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,from_date,to_date,day_wise,periods_json,reason,status,advisor_decision,advisor_note,hod_decision,hod_note,proof_name,created_at FROM od_requests WHERE student_id=? ORDER BY id DESC LIMIT 200',(student[0],))
    rows=c.fetchall(); conn.close()
    keys=['id','from_date','to_date','day_wise','periods_json','reason','status','advisor_decision','advisor_note','hod_decision','hod_note','proof_name','created_at']
    return jsonify({'ok':True,'requests':[dict(zip(keys,r)) for r in rows]})

def _od_scope_ok_for_user(req_class):
    role=session.get('role')
    if role=='admin' or session.get('is_hod'): return True
    if session.get('is_advisor') and session.get('advisor_class')==req_class: return True
    return False

@app.route('/api/od/pending')
@staff_required
def api_od_pending():
    role=session.get('role'); is_hod=bool(session.get('is_hod')); is_adv=bool(session.get('is_advisor')); adv_class=session.get('advisor_class')
    conn=_db(); c=conn.cursor()
    if role=='admin' or is_hod:
        c.execute("SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,proof_name,advisor_decision,advisor_note,hod_decision,hod_note,created_at FROM od_requests WHERE status IN ('pending_advisor','advisor_approved','pending_hod') ORDER BY id DESC LIMIT 300")
    elif is_adv:
        c.execute("SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,proof_name,advisor_decision,advisor_note,hod_decision,hod_note,created_at FROM od_requests WHERE class_name=? AND status IN ('pending_advisor','advisor_approved') ORDER BY id DESC LIMIT 300",(adv_class,))
    else:
        conn.close(); return jsonify({'ok':True,'requests':[],'note':'Only class advisor/HOD/Admin can review day OD requests.'})
    rows=c.fetchall(); conn.close()
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json','reason','status','proof_name','advisor_decision','advisor_note','hod_decision','hod_note','created_at']
    return jsonify({'ok':True,'requests':[dict(zip(keys,r)) for r in rows],'can_review_hod':bool(role=='admin' or is_hod),'can_review_advisor':bool(is_adv or role=='admin' or is_hod)})

def _apply_approved_od(conn, req):
    c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
    import datetime as dt
    try:
        d1=dt.date.fromisoformat(req['from_date']); d2=dt.date.fromisoformat(req['to_date'])
    except Exception: return
    cur=d1
    while cur<=d2:
        for per in range(1,8):
            c.execute("INSERT INTO od_records (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at) VALUES (?,?,?,?,?,?,?,?, 'OD',?,?,?)",
                      (req['id'], req['student_id'], req['reg_no'], req['student_name'], req['class_name'], cur.isoformat(), per, 'DAY_OD', 'Advisor+HOD Approved', 'od_workflow', now))
        cur += dt.timedelta(days=1)

@app.route('/api/od/review', methods=['POST'])
@staff_required
def api_od_review():
    data=request.get_json(silent=True) or {}
    rid=data.get('request_id'); action=(data.get('action') or '').strip().lower(); note=(data.get('note') or '').strip()
    if action not in ('advisor_approve','advisor_reject','hod_approve','hod_reject'): return jsonify({'ok':False,'error':'Invalid action'}), 400
    conn=_db(); c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
    c.execute('SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,proof_path FROM od_requests WHERE id=?',(rid,))
    row=c.fetchone()
    if not row: conn.close(); return jsonify({'ok':False,'error':'Request not found'}), 404
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json','reason','status','proof_path']
    req=dict(zip(keys,row))
    if not _od_scope_ok_for_user(req['class_name']) and session.get('role')!='admin': conn.close(); return jsonify({'ok':False,'error':'Not authorized for this class/request'}), 403
    is_hod=bool(session.get('is_hod')) or session.get('role')=='admin'; is_adv=bool(session.get('is_advisor')) or is_hod
    st=req['status']
    if action=='advisor_reject':
        if not is_adv: conn.close(); return jsonify({'ok':False,'error':'Advisor/HOD/Admin only'}), 403
        c.execute('UPDATE od_requests SET status="cancelled", advisor_code=?,advisor_decision="rejected",advisor_note=?,advisor_at=?,updated_at=? WHERE id=?',(session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='advisor_approve':
        if not (is_adv and (session.get('advisor_class')==req['class_name'] or is_hod or session.get('role')=='admin')): conn.close(); return jsonify({'ok':False,'error':'Only class advisor (own class), HOD, or Admin'}), 403
        if st!='pending_advisor': conn.close(); return jsonify({'ok':False,'error':f'Cannot advisor-approve from status {st}'}), 400
        c.execute('UPDATE od_requests SET status="advisor_approved", advisor_code=?,advisor_decision="approved",advisor_note=?,advisor_at=?,updated_at=? WHERE id=?',(session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='hod_reject':
        if not is_hod: conn.close(); return jsonify({'ok':False,'error':'HOD/Admin only'}), 403
        c.execute('UPDATE od_requests SET status="cancelled", hod_code=?,hod_decision="rejected",hod_note=?,hod_at=?,updated_at=? WHERE id=?',(session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='hod_approve':
        if not is_hod: conn.close(); return jsonify({'ok':False,'error':'HOD/Admin only'}), 403
        c.execute('SELECT status FROM od_requests WHERE id=?',(rid,)); st=c.fetchone()[0]
        if st!='advisor_approved': conn.close(); return jsonify({'ok':False,'error':'Day OD requires Advisor approval first, then HOD approval. Current: '+st}), 400
        c.execute('UPDATE od_requests SET status="od_approved", hod_code=?,hod_decision="approved",hod_note=?,hod_at=?,updated_at=? WHERE id=?',(session.get('staff_code') or session.get('username'), note, now, now, rid))
        req['status']='od_approved'; _apply_approved_od(conn, req)
    conn.commit(); conn.close()
    return jsonify({'ok':True,'request_id':rid,'action':action})

@app.route('/api/od/proof/<int:rid>')
@staff_required
def api_od_proof(rid):
    conn=_db(); c=conn.cursor(); c.execute('SELECT class_name,proof_path,proof_name FROM od_requests WHERE id=?',(rid,)); r=c.fetchone(); conn.close()
    if not r: abort(404)
    if not _od_scope_ok_for_user(r[0]) and session.get('role')!='admin': abort(403)
    if not r[1] or not os.path.exists(r[1]): abort(404)
    return send_file(r[1], as_attachment=True, download_name=r[2] or os.path.basename(r[1]))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
