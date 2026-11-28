import os
import io
import csv
import time
import ssl
import smtplib
import shutil
import threading
import sqlite3
import datetime
import json
import re
import secrets
import pickle
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlencode
from email.mime.text import MIMEText
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, url_for, session, flash
from werkzeug.utils import secure_filename
from markupsafe import Markup
from model import train_model_background, extract_embedding_for_image, load_model_if_exists, MODEL_PATH, predict_with_model, detect_faces_bboxes, crop_face_and_embed, face_capabilities
import hashlib
import cv2
import numpy as np
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# Optional .env loader (keeps secrets out of source control).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except Exception:
    pass

# --- Global Configurations ---
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('ERP_DB_PATH') or os.path.join(APP_DIR, "attendance.db")
DATASET_DIR = os.path.join(APP_DIR, "dataset")
TIMETABLE_DIR = os.path.join(APP_DIR, "timetables")
TIMETABLE_BACKUP_DIR = os.path.join(TIMETABLE_DIR, "backups")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
OD_PROOFS_DIR = os.path.join(UPLOADS_DIR, "od_proofs")
MEDICAL_CERTS_DIR = os.path.join(UPLOADS_DIR, "medical_certificates")
TIMETABLE_REF_DIR = os.path.join(UPLOADS_DIR, "timetables")
FACE_ATTENDANCE_DIR = os.path.join(UPLOADS_DIR, "face_attendance")
FACE_VIDEO_DIR = os.path.join(FACE_ATTENDANCE_DIR, "videos")
FACE_CROP_DIR = os.path.join(FACE_ATTENDANCE_DIR, "crops")
os.makedirs(DATASET_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OD_PROOFS_DIR, exist_ok=True)
os.makedirs(MEDICAL_CERTS_DIR, exist_ok=True)
os.makedirs(TIMETABLE_DIR, exist_ok=True)
os.makedirs(TIMETABLE_BACKUP_DIR, exist_ok=True)
os.makedirs(TIMETABLE_REF_DIR, exist_ok=True)
os.makedirs(FACE_VIDEO_DIR, exist_ok=True)
os.makedirs(FACE_CROP_DIR, exist_ok=True)
TRAIN_STATUS_FILE = os.path.join(APP_DIR, "train_status.json")
PROFILE_PHOTOS_DIR = os.path.join(UPLOADS_DIR, "profile_photos")
os.makedirs(PROFILE_PHOTOS_DIR, exist_ok=True)

COOLDOWN_SECONDS = 60
CCTV_URL = 0
current_source = 0
current_source_type = 'webcam'
cctv_running = False
cctv_stop_reason = 'Auto-marking disabled: use Staff Session draft->submit flow (Phase 3).'
ALLOWED_VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv', '.m4v', '.wmv'}
ALLOWED_TIMETABLE_REF_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}
MAX_OD_PROOF_MB = 50
FACE_VIDEO_MAX_MB = int(os.environ.get('ERP_FACE_VIDEO_MAX_MB') or 550)
FACE_WEBCAM_MAX_MB = int(os.environ.get('ERP_FACE_WEBCAM_MAX_MB') or 10)
FACE_MATCH_MIN_CONFIDENCE = float(os.environ.get('ERP_FACE_MIN_CONFIDENCE') or 0.55)
FACE_MATCH_REVIEW_CONFIDENCE = float(os.environ.get('ERP_FACE_REVIEW_CONFIDENCE') or 0.72)
FACE_UNKNOWN_SIMILARITY = float(os.environ.get('ERP_FACE_UNKNOWN_SIMILARITY') or 0.92)
FACE_VIDEO_SAMPLE_FPS = float(os.environ.get('ERP_FACE_SAMPLE_FPS') or 2.0)
FACE_VIDEO_MAX_SECONDS = int(os.environ.get('ERP_FACE_VIDEO_MAX_SECONDS') or 3600)
FACE_VIDEO_MAX_FACES = max(1, int(os.environ.get('ERP_FACE_VIDEO_MAX_FACES') or 80))
FACE_UNKNOWN_MAX_GROUPS = max(1, int(os.environ.get('ERP_FACE_UNKNOWN_MAX_GROUPS') or 300))
FACE_JOB_VIDEO_TTL_HOURS = int(os.environ.get('ERP_FACE_VIDEO_TTL_HOURS') or 24)
FACE_AUTOSCAN_STABLE = max(2, int(os.environ.get('ERP_FACE_AUTOSCAN_STABLE') or 3))
FACE_AUTOSCAN_COOLDOWN_SEC = max(30, min(60, int(os.environ.get('ERP_FACE_AUTOSCAN_COOLDOWN_SEC') or 45)))
_face_autoscan_state = {}  # job_id -> {sid: {'streak':n,'last_mark_ts':epoch,'last_seen_ts':epoch}}
PROTOTYPE_MODE = False  # data persistent (per user confirmation)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=24)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = (FACE_VIDEO_MAX_MB + 25) * 1024 * 1024


@app.errorhandler(413)
def _request_too_large(error):
    return jsonify(ok=False, error=f"Upload is too large. Maximum video size is {FACE_VIDEO_MAX_MB} MB."), 413


# ==============================================================================
# HELPERS
# ==============================================================================
def _db(): return sqlite3.connect(DB_PATH, timeout=15)
def _hash_pw(pw): return hashlib.sha256((pw or '').encode()).hexdigest()
def _norm(s): return re.sub(r'\s+', ' ', (s or '').strip())
def _upper(s): return _norm(s).upper()

STUDENT_ROLE_PRESETS = ['Representative','Ambassador','Club Lead','Other']
def _normalize_student_role(value):
    role = _norm(value)
    if not role:
        return ''
    if role.lower() in ('representative','rep','class representative','class rep'):
        return 'Representative'
    return role[:60]

def _is_rep_role(value):
    return _normalize_student_role(value).lower() == 'representative'

def rep_badge(value):
    if not _is_rep_role(value):
        return Markup('')
    return Markup('<span class="badge bg-primary ms-1" title="Representative">Rep</span>')

app.jinja_env.globals['rep_badge'] = rep_badge

UNMARKED_STATUS = '-'
ATTENDANCE_STATUSES = ('Present','Absent','OD','Late','N','Medical Leave',UNMARKED_STATUS)
ATTENDANCE_COUNTED_STATUSES = ('Present','Absent','OD','Late','N','Medical Leave')
ATTENDANCE_CREDITED_STATUSES = ('Present','Late','OD','Medical Leave')
ATTENDANCE_ABSENCE_STATUSES = ('Absent','N')
ATTENDANCE_STATUS_ALIASES = {
    'P':'Present','PRESENT':'Present',
    'A':'Absent','ABSENT':'Absent',
    'OD':'OD','ON DUTY':'OD','ON-DUTY':'OD',
    'L':'Late','LATE':'Late',
    'N':'N',
    'ML':'Medical Leave','MEDICAL':'Medical Leave','MEDICAL LEAVE':'Medical Leave',
    '-':UNMARKED_STATUS,'UNMARKED':UNMARKED_STATUS,'NOT MARKED':UNMARKED_STATUS,'NOTMARKED':UNMARKED_STATUS,
}
ATTENDANCE_COUNTED_SQL = "('present','absent','od','late','n','medical leave')"
ATTENDANCE_CREDITED_SQL = "('present','od','late','medical leave')"

def _normalize_attendance_status(value, default=UNMARKED_STATUS):
    key=_norm(value).upper()
    if not key: return default
    return ATTENDANCE_STATUS_ALIASES.get(key, default)

def _is_attendance_counted(status):
    return _normalize_attendance_status(status) in ATTENDANCE_COUNTED_STATUSES

def _is_attendance_credited(status):
    return _normalize_attendance_status(status) in ATTENDANCE_CREDITED_STATUSES

def _is_attendance_absence(status):
    return _normalize_attendance_status(status) in ATTENDANCE_ABSENCE_STATUSES

def _normalize_dob(dob):
    dob = _norm(dob)
    if not dob: return ''
    for fmt in ('%Y-%m-%d','%d-%m-%Y','%d/%m/%Y','%Y/%m/%d','%Y%m%d'):
        try: return datetime.datetime.strptime(dob, fmt).strftime('%Y-%m-%d')
        except Exception: pass
    return dob

def batch_label(batch_start_year):
    try:
        y=int(batch_start_year)
        return f'{y}-{y+4}'
    except Exception:
        return ''

def _batch_start_year_from_label(value):
    v=_norm(value)
    m=re.fullmatch(r'(\d{4})\s*-\s*(\d{4})', v)
    if not m: return None
    start,end=int(m.group(1)),int(m.group(2))
    if end!=start+4: return None
    return start

def default_batch_sem_dates(batch_start_year, sem_no):
    """Default batch-sem calendar: odd 01/08-31/12, even 01/01-20/04 (gap 21/04-31/07 intended)."""
    y=int(batch_start_year); sem=int(sem_no)
    if sem<1 or sem>8: raise ValueError('sem_no must be 1..8')
    academic_year=y + ((sem-1)//2)
    if sem % 2 == 1:
        start=datetime.date(academic_year,8,1)
        end=datetime.date(academic_year,12,31)
    else:
        start=datetime.date(academic_year+1,1,1)
        end=datetime.date(academic_year+1,4,20)
    return start.isoformat(), end.isoformat()

def batch_sem_label(batch_start_year, sem_no):
    return f"{batch_label(batch_start_year)} Sem {int(sem_no)}"

def cohort_key_for(batch_start_year, section):
    sec=_norm(section).upper() or 'A'
    sec=re.sub(r'[^A-Z0-9]','',sec)[:4] or 'A'
    return f"{int(batch_start_year)}:{sec}"

def class_name_for_cohort(batch_start_year, sem_no, section):
    yy=str(int(batch_start_year) % 100).zfill(2)
    sec=_norm(section).upper() or 'A'
    sec=re.sub(r'[^A-Z0-9]','',sec)[:4] or 'A'
    return f"{yy}IT{int(sem_no)}{sec}"

def parse_cohort_class(class_name):
    name=canonical_class_name(class_name)
    m=CLASS_NAME_RE.match(name)
    if not m: return None
    start=2000+int(m.group(1)); sem=int(m.group(2)); section=m.group(3).upper()
    return {'class_name':name,'batch_start_year':start,'sem_no':sem,'section':section,'cohort_key':cohort_key_for(start,section),'batch_label':batch_label(start)}

def _class_year_info(class_name):
    """Infer batch/study year from class like 24IT5A. Rule: sem1-2=Y1,3-4=Y2,5-6=Y3,7-8=Y4."""
    info=parse_cohort_class(class_name)
    if not info:
        return '', ''
    start=info['batch_start_year']; sem=info['sem_no']
    study = (sem + 1) // 2
    return f'{start}-{start + 4}', study

def _normalize_batch_year(value):
    value = _norm(value)
    if not value:
        return ''
    match = re.fullmatch(r'(\d{4})\s*-\s*(\d{4})', value)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if end != start + 4:
        return None
    return f'{start}-{end}'

def _normalize_study_year(value):
    value = _norm(value)
    if not value:
        return ''
    try:
        year = int(float(value))
    except Exception:
        return None
    return str(year) if 1 <= year <= 4 else None

def _ensure_student_year_columns(c):
    _ensure_column(c, 'students', 'batch_year', 'TEXT')
    _ensure_column(c, 'students', 'study_year', 'INTEGER')

def _ensure_student_role_column(c):
    _ensure_column(c, 'students', 'student_role', 'TEXT')

def _backfill_student_years(c):
    _ensure_student_year_columns(c)
    rows = c.execute('SELECT id,class,batch_year,study_year FROM students').fetchall()
    for sid, cls, batch, study in rows:
        derived_batch, derived_study = _class_year_info(cls)
        updates, params = [], []
        if (batch is None or str(batch).strip() == '') and derived_batch:
            updates.append('batch_year=?'); params.append(derived_batch)
        if (study is None or str(study).strip() == '') and derived_study:
            updates.append('study_year=?'); params.append(int(derived_study))
        if updates:
            params.append(sid)
            c.execute('UPDATE students SET ' + ','.join(updates) + ' WHERE id=?', params)

def _staff_password(display_name):
    # password rule: name only (no Mr/Ms/Dr/initials), ALL CAPS
    name = re.sub(r'^(dr\.?|mr\.?|ms\.?|mrs\.?|prof\.?)\s*', '', _norm(display_name), flags=re.I)
    parts = [p for p in re.split(r'\s+', name) if not re.fullmatch(r'[A-Za-z]\.?', p)]
    return ' '.join(parts).strip().upper()

def _env(name, default=''):
    return (os.environ.get(name) or default or '').strip()

# Phase 1 class registry and normalization
CLASS_REGISTRY = ['23IT7A', '23IT7B', '24IT5A', '24IT5B', '25IT3A', '25IT3B']
LEGACY_CLASS_MAP = {'24IIT5A': '24IT5A', '24IIT5B': '24IT5B', '24ITR7A': '23IT7A'}
CLASS_NAME_RE = re.compile(r'^(\d{2})IT([1-8])([A-Z])$')
BATCH_REGISTRY = [
    {'batch_start_year':2023,'label':'2023-2027'},
    {'batch_start_year':2024,'label':'2024-2028'},
    {'batch_start_year':2025,'label':'2025-2029'},
    {'batch_start_year':2026,'label':'2026-2030'},
]
BATCH_START_YEARS = [b['batch_start_year'] for b in BATCH_REGISTRY]

def canonical_class_name(class_name: str) -> str:
    name = _norm(class_name).upper()
    return LEGACY_CLASS_MAP.get(name, name)

def is_valid_class_name(class_name: str) -> bool:
    return bool(CLASS_NAME_RE.match(canonical_class_name(class_name)))

def get_classes(include_legacy: bool = False, include_cohorts: bool = True):
    """Return canonical class names from registry, active cohort current classes, and valid DB classes."""
    classes = list(CLASS_REGISTRY)
    if include_cohorts:
        try:
            for r in cohort_rows(active_only=True):
                cn=canonical_class_name(r.get('current_class_name'))
                if cn and cn not in classes: classes.append(cn)
        except Exception: pass
    try:
        conn = _db(); c = conn.cursor()
        c.execute("SELECT DISTINCT class FROM students WHERE class IS NOT NULL AND TRIM(class)!='' ORDER BY class")
        for (name,) in c.fetchall():
            n = canonical_class_name(name)
            if n and (include_legacy or is_valid_class_name(n)) and n not in classes:
                classes.append(n)
        conn.close()
    except Exception:
        pass
    if include_legacy:
        try:
            for name in sorted(os.listdir(TIMETABLE_DIR)):
                if name.lower().endswith('.json'):
                    n = canonical_class_name(os.path.splitext(name)[0])
                    if n and n not in classes:
                        classes.append(n)
        except Exception:
            pass
    valid = sorted(set([x for x in classes if is_valid_class_name(x)]))
    def _k(x):
        info=parse_cohort_class(x)
        if info: return (0,info['batch_start_year'],info['sem_no'],info['section'],x)
        return (1,9999,99,x,x)
    return sorted(valid, key=_k)

def _timetable_version_row(class_name, on_date=None):
    """Return the effective timetable version for a class/date, if one exists."""
    cls=_norm(class_name)
    date_str=(on_date or datetime.date.today().isoformat()).strip()
    if not cls or not date_str: return None
    conn=_db(); c=conn.cursor()
    try:
        c.execute("""SELECT id,class_name,semester_id,version_no,effective_from,effective_to,
                            timetable_json,reference_path,reference_name,is_active,created_by,created_at,updated_at,notes,hidden,hidden_at,hidden_by
                     FROM timetable_versions
                     WHERE upper(class_name)=upper(?) AND COALESCE(hidden,0)=0
                       AND effective_from<=?
                       AND (effective_to IS NULL OR effective_to='' OR effective_to>=?)
                     ORDER BY effective_from DESC, version_no DESC LIMIT 1""",
                  (cls,date_str,date_str))
        row=c.fetchone()
        if not row: return None
        keys=['id','class_name','semester_id','version_no','effective_from','effective_to',
              'timetable_json','reference_path','reference_name','is_active','created_by','created_at','updated_at','notes','hidden','hidden_at','hidden_by']
        return dict(zip(keys,row))
    except Exception:
        return None
    finally:
        conn.close()

def _timetable_version_id(class_name, on_date=None):
    version=_timetable_version_row(class_name,on_date)
    try:
        return int(version['id']) if version and version.get('id') is not None else None
    except Exception:
        return None

def load_timetable(class_name, on_date=None):
    """Load the timetable effective on a date; fall back to the legacy JSON file."""
    version=_timetable_version_row(class_name,on_date)
    if version:
        try: return json.loads(version.get('timetable_json') or '{}')
        except Exception: pass
    path = os.path.join(TIMETABLE_DIR, f"{class_name}.json")
    if not os.path.exists(path): return None
    with open(path, 'r', encoding='utf-8-sig') as f: return json.load(f)

def all_timetable_staff():
    staff = {}; advisors = {}
    for cls in get_classes():
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


def _migrate_phase1_class_registry(c):
    mapping = dict(LEGACY_CLASS_MAP)
    targets = [
        ('students','class'), ('attendance','class_name'), ('attendance_entries','class_name'),
        ('attendance_edits','class_name'), ('attendance_audit','class_name'),
        ('face_attendance','class_name'), ('face_attendance_records','class_name'),
        ('elective_offerings','class_name'), ('timetable_versions','class_name'),
        ('timetable_snapshots','class_name'), ('period_locks','class_name'),
        ('od_requests','class_name'), ('medical_requests','class_name'),
        ('alerts','target_class'), ('users','advisor_class'),
    ]
    for table,col in targets:
        try:
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not c.fetchone(): continue
            c.execute(f"PRAGMA table_info({table})"); cols=[r[1] for r in c.fetchall()]
            if col not in cols: continue
            for old,new in mapping.items():
                c.execute(f"UPDATE {table} SET {col}=? WHERE upper(trim({col}))=upper(?)", (new,old))
        except Exception:
            pass
    for table,col in [('timetable_versions','timetable_json'),('timetable_snapshots','snapshot_json')]:
        try:
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not c.fetchone(): continue
            c.execute(f"SELECT rowid,{col} FROM {table}")
            for rowid,val in c.fetchall():
                if not val: continue
                nv=val
                for old,new in mapping.items(): nv=nv.replace(old,new)
                if nv!=val: c.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (nv,rowid))
        except Exception:
            pass
    try:
        c.execute("UPDATE users SET active=1, deleted_at=NULL WHERE role IN ('staff','hod','admin') AND deleted_at IS NOT NULL AND deleted_at!=''")
    except Exception:
        pass
    try:
        c.execute("SELECT id,class,batch_year,study_year FROM students")
        for sid,cls,by,sy in c.fetchall():
            n=canonical_class_name(cls); iby,isy=_class_year_info(n)
            if n!=cls or (not by and iby) or (not sy and isy):
                c.execute("UPDATE students SET class=?, batch_year=COALESCE(NULLIF(batch_year,''),?), study_year=CASE WHEN study_year IS NULL OR study_year='' THEN ? ELSE study_year END WHERE id=?", (n,iby,isy,sid))
    except Exception:
        pass
def init_db():
    conn = _db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, roll TEXT, class TEXT, section TEXT, reg_no TEXT, dob TEXT, created_at TEXT
                )""")
    for col,coldef in [('dob','TEXT'),('reg_no','TEXT'),('active','INTEGER DEFAULT 1'),('email','TEXT'),('phone','TEXT'),('gender','TEXT'),('blood_group','TEXT'),('guardian_name','TEXT'),('guardian_phone','TEXT'),('address','TEXT'),('batch_year','TEXT'),('study_year','INTEGER'),('student_role','TEXT')]:
        _ensure_column(c,'students',col,coldef)
    _backfill_student_years(c)
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, name TEXT, timestamp TEXT, source TEXT DEFAULT 'auto', marked_by TEXT
                )""")
    for col,coldef in [('source',"TEXT DEFAULT 'auto'"),('marked_by','TEXT')]: _ensure_column(c,'attendance',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL, student_id INTEGER, created_at TEXT
                )""")
    for col,coldef in [('student_id','INTEGER'),('staff_code','TEXT'),('display_name','TEXT'),('is_hod','INTEGER DEFAULT 0'),('is_advisor','INTEGER DEFAULT 0'),('advisor_class','TEXT'),('dob','TEXT'),('phone','TEXT'),('email','TEXT'),('active','INTEGER DEFAULT 1'),('gender','TEXT'),('designation','TEXT'),('department','TEXT'),('qualification','TEXT'),('address','TEXT'),('date_of_joining','TEXT'),('deleted_at','TEXT')]:
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
    for col,coldef in [('timetable_version_id','INTEGER'),('duration_minutes','INTEGER')]:
        _ensure_column(c,'attendance_sessions',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS attendance_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER, student_id INTEGER, reg_no TEXT, student_name TEXT,
                    status TEXT, confidence REAL, flagged INTEGER DEFAULT 0, source TEXT,
                    marked_by TEXT, updated_at TEXT, elective_label TEXT,
                    UNIQUE(session_id, student_id)
                )""")
    _ensure_column(c,'attendance_entries','elective_label','TEXT')
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
    c.execute("""CREATE TABLE IF NOT EXISTS medical_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER, reg_no TEXT, student_name TEXT, class_name TEXT,
                    semester_id INTEGER, from_date TEXT, to_date TEXT, periods_json TEXT,
                    day_wise INTEGER DEFAULT 1, reason TEXT,
                    certificate_path TEXT, certificate_name TEXT,
                    status TEXT DEFAULT 'pending_advisor',
                    advisor_code TEXT, advisor_decision TEXT, advisor_note TEXT, advisor_at TEXT,
                    hod_code TEXT, hod_decision TEXT, hod_note TEXT, hod_at TEXT,
                    created_at TEXT, updated_at TEXT
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_medical_requests_student_sem ON medical_requests(student_id, semester_id, status)")
    c.execute("""CREATE TABLE IF NOT EXISTS medical_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER, student_id INTEGER, reg_no TEXT, student_name TEXT, class_name TEXT,
                    date TEXT, period INTEGER, subject TEXT, status TEXT DEFAULT 'Medical Leave',
                    issued_by TEXT, issued_role TEXT, created_at TEXT
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_medical_records_request ON medical_records(request_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS hod_staff_permissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hod_code TEXT, staff_code TEXT, permission TEXT, active INTEGER DEFAULT 1,
                    granted_at TEXT, revoked_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS academic_calendar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, date TEXT NOT NULL, end_date TEXT,
                    type TEXT NOT NULL, affects_attendance INTEGER DEFAULT 1,
                    class_name TEXT, notes TEXT, created_by TEXT, created_at TEXT
                )""")
    for col,coldef in [('end_date','TEXT'),('affects_attendance','INTEGER DEFAULT 1'),('class_name','TEXT'),('notes','TEXT'),('created_by','TEXT')]:
        _ensure_column(c,'academic_calendar',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, updated_by TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS semesters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL, start_date TEXT, end_date TEXT,
                    is_current INTEGER DEFAULT 0,
                    created_by TEXT, created_at TEXT, updated_at TEXT
                )""")
    for col,coldef in [('batch_start_year','INTEGER'),('sem_no','INTEGER'),('is_auto','INTEGER DEFAULT 1')]:
        _ensure_column(c,'semesters',col,coldef)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_semesters_batch_sem ON semesters(batch_start_year, sem_no) WHERE batch_start_year IS NOT NULL AND sem_no IS NOT NULL")
    c.execute("""CREATE TABLE IF NOT EXISTS class_cohorts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cohort_key TEXT NOT NULL UNIQUE,
                    batch_start_year INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    current_sem_no INTEGER NOT NULL,
                    current_class_name TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TEXT, updated_at TEXT
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_class_cohorts_current_class ON class_cohorts(current_class_name)")
    c.execute("""CREATE TABLE IF NOT EXISTS timetable_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT NOT NULL,
                    semester_id INTEGER,
                    version_no INTEGER NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    timetable_json TEXT NOT NULL,
                    reference_path TEXT,
                    reference_name TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_by TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    notes TEXT,
                    UNIQUE(class_name, semester_id, version_no)
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_timetable_versions_lookup ON timetable_versions(class_name, effective_from, effective_to)")
    for col,coldef in [('hidden','INTEGER DEFAULT 0'),('hidden_at','TEXT'),('hidden_by','TEXT')]:
        _ensure_column(c,'timetable_versions',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS attendance_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_role TEXT, actor_user TEXT, actor_name TEXT,
                    entity_type TEXT, entity_id TEXT, action TEXT,
                    old_value TEXT, new_value TEXT, details TEXT, ts TEXT
                 )""")
    for sql in ('CREATE INDEX IF NOT EXISTS idx_audit_ts ON attendance_audit_log(ts)',
                'CREATE INDEX IF NOT EXISTS idx_audit_entity ON attendance_audit_log(entity_type, action)',
                'CREATE INDEX IF NOT EXISTS idx_audit_actor ON attendance_audit_log(actor_user)'):
        c.execute(sql)
    c.execute("""CREATE TABLE IF NOT EXISTS student_electives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL, class_name TEXT NOT NULL,
                    timetable_version_id INTEGER,
                    slot_key TEXT NOT NULL, option_label TEXT NOT NULL,
                    updated_at TEXT, updated_by TEXT,
                    UNIQUE(student_id, class_name, timetable_version_id, slot_key)
                )""")
    try:
        cols=[r[1] for r in c.execute("PRAGMA table_info(student_electives)").fetchall()]
        if 'timetable_version_id' not in cols:
            c.execute("DROP TABLE IF EXISTS student_electives_old")
            c.execute("ALTER TABLE student_electives RENAME TO student_electives_old")
            c.execute("""CREATE TABLE student_electives (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            student_id INTEGER NOT NULL, class_name TEXT NOT NULL,
                            timetable_version_id INTEGER,
                            slot_key TEXT NOT NULL, option_label TEXT NOT NULL,
                            updated_at TEXT, updated_by TEXT,
                            UNIQUE(student_id, class_name, timetable_version_id, slot_key)
                        )""")
            c.execute("""INSERT INTO student_electives
                         (id,student_id,class_name,timetable_version_id,slot_key,option_label,updated_at,updated_by)
                         SELECT id,student_id,class_name,NULL,slot_key,option_label,updated_at,updated_by
                         FROM student_electives_old""")
            c.execute("DROP TABLE student_electives_old")
    except Exception:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_student_electives_lookup ON student_electives(student_id,class_name,timetable_version_id,slot_key)")
    c.execute("""CREATE TABLE IF NOT EXISTS elective_offerings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    class_name TEXT NOT NULL,
                    timetable_version_id INTEGER NOT NULL,
                    slot_key TEXT NOT NULL,
                    option_label TEXT NOT NULL,
                    staff_codes TEXT DEFAULT '',
                    updated_at TEXT, updated_by TEXT,
                    UNIQUE(class_name,timetable_version_id,slot_key,option_label)
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_elective_offerings_lookup ON elective_offerings(class_name,timetable_version_id,slot_key)")
    c.execute("""CREATE TABLE IF NOT EXISTS student_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL, alert_type TEXT, title TEXT, body TEXT,
                    source_ref TEXT, created_at TEXT, read_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS staff_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_code TEXT, recipient_user_id INTEGER,
                    title TEXT, body TEXT, source_ref TEXT,
                    created_at TEXT, read_at TEXT
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_staff_alerts_recipient ON staff_alerts(recipient_code, read_at, created_at)")
    c.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER, student_id INTEGER, channel TEXT, recipient TEXT,
                    subject TEXT, body TEXT, event_type TEXT,
                    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                    last_error TEXT, created_at TEXT, sent_at TEXT
                )""")
    for col,coldef in [('alert_id','INTEGER'),('attempts','INTEGER DEFAULT 0'),('last_error','TEXT'),('sent_at','TEXT'),('claimed_at','TEXT')]:
        _ensure_column(c,'notification_outbox',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL, endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT, auth TEXT, user_agent TEXT, active INTEGER DEFAULT 1,
                    created_at TEXT
                )""")
    for col,coldef in [('active','INTEGER DEFAULT 1'),('user_agent','TEXT')]:
        _ensure_column(c,'push_subscriptions',col,coldef)
    c.execute("""CREATE TABLE IF NOT EXISTS face_attendance_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    period INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    progress INTEGER DEFAULT 0,
                    video_path TEXT,
                    video_size INTEGER DEFAULT 0,
                    video_deleted INTEGER DEFAULT 0,
                    frames_scanned INTEGER DEFAULT 0,
                    faces_detected INTEGER DEFAULT 0,
                    matched_students INTEGER DEFAULT 0,
                    unknown_faces INTEGER DEFAULT 0,
                    error TEXT,
                    notes TEXT,
                    created_by TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    reviewed_at TEXT
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_face_jobs_created ON face_attendance_jobs(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_face_jobs_status ON face_attendance_jobs(status)")
    c.execute("""CREATE TABLE IF NOT EXISTS face_attendance_faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    face_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    student_id INTEGER,
                    student_name TEXT,
                    reg_no TEXT,
                    match_confidence REAL DEFAULT 0,
                    sample_count INTEGER DEFAULT 1,
                    flagged INTEGER DEFAULT 0,
                    bbox TEXT,
                    frame_time REAL,
                    crop_path TEXT,
                    assigned_by TEXT,
                    assigned_at TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(job_id, face_key)
                )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_face_faces_job ON face_attendance_faces(job_id)")
    c.execute("UPDATE attendance_entries SET status='-' WHERE source='auto_absent' AND status='Absent'")
    c.execute("UPDATE attendance_sessions SET staff_name='System (unmarked)', notes='Auto-created for unmarked period' WHERE source='auto_absent' AND staff_name='System (default absent)'")
    c.execute("UPDATE app_settings SET value='22' WHERE key='attendance_cutoff_hour' AND value='18'")
    conn.commit(); conn.close()
    if not os.path.exists(TRAIN_STATUS_FILE): write_train_status({"running": False, "progress": 0, "message": "No training yet."})

def sync_staff_from_timetables(reset_passwords=False):
    """Create/update staff users from timetable data. Preserves passwords/profile/active/deleted."""
    staff_map, advisors = all_timetable_staff()
    conn = _db(); c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    added = updated = 0
    for code, info in staff_map.items():
        n=info.get('name') if isinstance(info,dict) else info; display=_norm((n.get('name') if isinstance(n,dict) else n) or code)
        username = code.upper(); is_hod = 1 if code == HOD_STAFF_CODE else 0; adv = advisors.get(code)
        adv_class = canonical_class_name((adv or {}).get('class') or '')
        c.execute('SELECT id,password,active,deleted_at,role,display_name FROM users WHERE upper(username)=upper(?) OR upper(staff_code)=upper(?) ORDER BY id LIMIT 1', (username, code))
        row = c.fetchone()
        if not row:
            c.execute('INSERT INTO users (username,password,role,staff_code,display_name,is_hod,is_advisor,advisor_class,active,deleted_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (username, _hash_pw(_staff_password(display)), 'staff', code, display, is_hod, 1 if adv else 0, adv_class or None, 1, None, now))
            added += 1
        else:
            uid=row[0]
            role=(row[4] or 'staff')
            if role not in ('staff','hod','admin'):
                continue
            keep_display=_norm(row[5]) or display
            if reset_passwords:
                c.execute('UPDATE users SET password=? WHERE id=?', (_hash_pw(_staff_password(keep_display)), uid))
            c.execute('UPDATE users SET staff_code=?,display_name=?,is_hod=?,is_advisor=?,advisor_class=?,deleted_at=NULL WHERE id=?',
                      (code, keep_display, is_hod, 1 if adv else 0, adv_class or None, uid))
            updated += 1
    conn.commit(); conn.close()
    return {'added': added, 'updated': updated, 'staff': len(staff_map), 'advisors': len(advisors)}

def seed_defaults():
    conn = _db(); c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    c.execute('SELECT id FROM users WHERE username=?', ('ADMIN',))
    if not c.fetchone():
        c.execute('INSERT INTO users (username,password,role,display_name,is_hod,is_advisor,active,created_at) VALUES (?,?,?,?,?,?,?,?)',
                  ('ADMIN', _hash_pw('ADMIN123'), 'admin', 'Administrator', 0, 0, 1, now))
    else:
        c.execute("UPDATE users SET role='admin', active=1 WHERE username='ADMIN'")
    defaults = {
        'semester_label': 'Semester 5', 'semester_start': '', 'semester_end': '',
        'defaulter_threshold': '75', 'enforce_calendar_holidays': '1',
        'low_attendance_alerts': '1', 'low_attendance_cooldown_days': '7',
        'notify_email': '1', 'notify_sms': '1', 'notify_push': '1',
        'email_absent': '1', 'email_od': '1', 'email_low_attendance': '1',
        'sms_absent': '1', 'sms_od': '0', 'sms_low_attendance': '0',
        'push_absent': '1', 'push_od': '1', 'push_low_attendance': '1',
        'attendance_cutoff_hour': '22', 'attendance_start_hour': '6', 'attendance_window_days': '15'
    }
    for k, v in defaults.items():
        c.execute('INSERT OR IGNORE INTO app_settings (key,value,updated_at,updated_by) VALUES (?,?,?,?)', (k, v, now, 'system'))
    c.execute("DELETE FROM users WHERE username IN ('swathi','staff','student')")
    conn.commit(); conn.close()
    sync_staff_from_timetables(reset_passwords=False)

def ensure_semester_seed():
    """Seed legacy global semester and batch-relative S1-S8 rows for known batches."""
    conn=_db(); c=conn.cursor()
    try:
        c.execute('SELECT COUNT(*) FROM semesters'); n=int(c.fetchone()[0] or 0)
        if not n:
            c.execute("SELECT key,value FROM app_settings WHERE key IN ('semester_label','semester_start','semester_end')")
            vals=dict(c.fetchall())
            label=(vals.get('semester_label') or 'Semester 1').strip() or 'Semester 1'
            start=(vals.get('semester_start') or '').strip(); end=(vals.get('semester_end') or '').strip()
            now=datetime.datetime.utcnow().isoformat()
            c.execute('INSERT INTO semesters (label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto) VALUES (?,?,?,1,?,?,?,NULL,NULL,0)',
                      (label,start,end,'system',now,now))
            conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()
    for by in BATCH_START_YEARS:
        try: ensure_batch_semesters(by, actor='system')
        except Exception: pass

def seed_timetable_versions():
    """Migrate existing timetable JSON files into version 1 for the current semester."""
    conn=_db(); c=conn.cursor()
    try:
        c.execute("SELECT id,start_date,end_date FROM semesters WHERE is_current=1 ORDER BY id DESC LIMIT 1")
        sem=c.fetchone()
        if not sem: return
        sem_id,sem_start,sem_end=sem
        today=datetime.date.today().isoformat()
        effective_from=(sem_start or today).strip() or today
        effective_to=(sem_end or '').strip() or None
        now=datetime.datetime.utcnow().isoformat()
        for cls in get_classes():
            c.execute("SELECT COUNT(*) FROM timetable_versions WHERE upper(class_name)=upper(?) AND semester_id=?", (cls,sem_id))
            if int(c.fetchone()[0] or 0): continue
            path=os.path.join(TIMETABLE_DIR,f"{cls}.json")
            if not os.path.exists(path): continue
            try:
                with open(path,'r',encoding='utf-8-sig') as f: data=json.load(f)
            except Exception:
                continue
            c.execute("""INSERT INTO timetable_versions
                         (class_name,semester_id,version_no,effective_from,effective_to,timetable_json,
                          is_active,created_by,created_at,updated_at,notes)
                         VALUES (?,?,1,?,?,?,1,'migration',?,?,?)""",
                      (cls,sem_id,effective_from,effective_to,json.dumps(data,ensure_ascii=False),now,now,
                       'Seeded from timetables/%s.json' % cls))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

def _infer_batch_start_year_from_class(class_name):
    info=parse_cohort_class(class_name)
    return info.get('batch_start_year') if info else None

def ensure_class_cohorts_seed():
    """Seed stable cohort rows from registry/students/classes without rewriting historical records."""
    now=datetime.datetime.utcnow().isoformat()
    conn=_db(); c=conn.cursor()
    try:
        names=set(list(CLASS_REGISTRY))
        try:
            c.execute("SELECT DISTINCT class FROM students WHERE class IS NOT NULL AND TRIM(class)!=''")
            names.update([canonical_class_name(r[0]) for r in c.fetchall() if r and r[0]])
        except Exception: pass
        try:
            c.execute("SELECT DISTINCT class_name FROM timetable_versions WHERE class_name IS NOT NULL AND TRIM(class_name)!=''")
            names.update([canonical_class_name(r[0]) for r in c.fetchall() if r and r[0]])
        except Exception: pass
        try:
            for fn in os.listdir(TIMETABLE_DIR):
                if fn.lower().endswith('.json'): names.add(canonical_class_name(os.path.splitext(fn)[0]))
        except Exception: pass
        for name in sorted(names):
            info=parse_cohort_class(name)
            if not info: continue
            by, sec = info['batch_start_year'], info['section']
            ck=cohort_key_for(by,sec)
            cur_sem=current_sem_for_batch(by) or info['sem_no']
            cur_name=class_name_for_cohort(by,cur_sem,sec)
            c.execute("""INSERT INTO class_cohorts(cohort_key,batch_start_year,section,current_sem_no,current_class_name,active,created_at,updated_at)
                         VALUES (?,?,?,?,?,1,?,?)
                         ON CONFLICT(cohort_key) DO UPDATE SET
                           batch_start_year=excluded.batch_start_year,
                           section=excluded.section,
                           current_sem_no=CASE WHEN COALESCE(class_cohorts.current_sem_no,0)=0 THEN excluded.current_sem_no ELSE class_cohorts.current_sem_no END,
                           current_class_name=CASE WHEN COALESCE(class_cohorts.current_class_name,'')='' THEN excluded.current_class_name ELSE class_cohorts.current_class_name END,
                           updated_at=excluded.updated_at""",
                      (ck,by,sec,cur_sem,cur_name,now,now))
            try: ensure_batch_semesters(by, actor='migration')
            except Exception: pass
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()

def migrate_semester_ids_best_effort():
    """Map existing global semester references to batch-sem rows when derivable; never rewrite historical class_name."""
    conn=_db(); c=conn.cursor()
    try:
        # timetable_versions: infer batch from class_name, keep version row's class_name as historical
        c.execute("SELECT id,class_name,semester_id,effective_from FROM timetable_versions")
        for vid, cls, sem_id, eff in c.fetchall():
            info=parse_cohort_class(cls)
            if not info: continue
            by=info['batch_start_year']
            desired_sem_no=int(info.get('sem_no') or 0) or int(current_sem_for_batch(by,on_date=eff or datetime.date.today().isoformat()))
            target=get_or_create_batch_semester(by,desired_sem_no,actor='migration')
            if target and target.get('id') and int(target.get('id'))!=int(sem_id or 0):
                c.execute("UPDATE timetable_versions SET semester_id=? WHERE id=?", (int(target['id']), vid))
        # medical_requests: prefer class_name batch, fallback to student's current class
        c.execute("SELECT id,class_name,student_id,semester_id,from_date FROM medical_requests")
        for rid, cls, sid, sem_id, from_date in c.fetchall():
            info=parse_cohort_class(cls)
            if not info and sid:
                try:
                    c.execute("SELECT class FROM students WHERE id=?",(sid,))
                    sr=c.fetchone(); info=parse_cohort_class(sr[0]) if sr and sr[0] else None
                except Exception: info=None
            if not info: continue
            by=info['batch_start_year']
            old=_semester_by_id(sem_id) if sem_id else None
            if old and old.get('batch_start_year')==by and old.get('sem_no'):
                continue
            sn=current_sem_for_batch(by,on_date=from_date or datetime.date.today().isoformat())
            sem=get_or_create_batch_semester(by,sn,actor='migration')
            if sem and sem.get('id'):
                c.execute("UPDATE medical_requests SET semester_id=? WHERE id=?", (int(sem['id']), rid))
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
    finally:
        conn.close()

init_db(); seed_defaults(); ensure_semester_seed(); seed_timetable_versions(); ensure_class_cohorts_seed(); migrate_semester_ids_best_effort()

def cohort_rows(batch_start_year=None, active_only=True):
    conn=_db(); c=conn.cursor(); rows=[]
    try:
        q="SELECT id,cohort_key,batch_start_year,section,current_sem_no,current_class_name,active,created_at,updated_at FROM class_cohorts WHERE 1=1"
        params=[]
        if batch_start_year is not None:
            q+=" AND batch_start_year=?"; params.append(int(batch_start_year))
        if active_only:
            q+=" AND COALESCE(active,1)=1"
        q+=" ORDER BY batch_start_year DESC, section ASC, id ASC"
        c.execute(q,params); rows=c.fetchall()
    except Exception:
        rows=[]
    finally:
        conn.close()
    keys=['id','cohort_key','batch_start_year','section','current_sem_no','current_class_name','active','created_at','updated_at']
    out=[]
    for r in rows:
        d=dict(zip(keys,r))
        d['active']=int(d.get('active') or 0)
        d['batch_label']=batch_label(d.get('batch_start_year'))
        out.append(d)
    return out

def _rollover_targets(batch_start_year, to_sem_no=None):
    by=int(batch_start_year)
    to_sem=_normalize_sem_no(to_sem_no) if to_sem_no not in (None,'') else (current_sem_for_batch(by) or 1)
    ensure_batch_semesters(by, actor=session.get('username') if session else 'system')
    rows=cohort_rows(batch_start_year=by, active_only=True)
    targets=[]
    for r in rows:
        frm=_normalize_sem_no(r.get('current_sem_no')) or current_sem_for_batch(by)
        sec=_norm(r.get('section')).upper() or 'A'
        from_class=r.get('current_class_name') or class_name_for_cohort(by,frm,sec)
        to_class=class_name_for_cohort(by,to_sem,sec)
        targets.append({**r,'from_sem_no':frm,'to_sem_no':to_sem,'from_class':from_class,'to_class':to_class,'cohort_key':r.get('cohort_key') or cohort_key_for(by,sec)})
    return to_sem, targets

def _rollover_preview(batch_start_year, to_sem_no=None):
    to_sem, targets=_rollover_targets(batch_start_year, to_sem_no)
    conn=_db(); c=conn.cursor()
    preview=[]
    try:
        for t in targets:
            frm=t['from_class']; to=t['to_class']
            c.execute("SELECT COUNT(*) FROM students WHERE upper(class)=upper(?)",(frm,))
            students=int(c.fetchone()[0] or 0)
            c.execute("SELECT COUNT(*) FROM users WHERE role IN ('staff','hod','admin') AND upper(advisor_class)=upper(?)",(frm,))
            advisors=int(c.fetchone()[0] or 0)
            c.execute("SELECT COUNT(*) FROM timetable_versions WHERE upper(class_name)=upper(?)",(frm,))
            tt_versions=int(c.fetchone()[0] or 0)
            tt_file=os.path.exists(os.path.join(TIMETABLE_DIR,f"{frm}.json"))
            preview.append({**t,'students':students,'advisor_users':advisors,'timetable_versions':tt_versions,'timetable_file':tt_file,'will_change':frm!=to})
    finally:
        conn.close()
    return {'ok':True,'batch_start_year':int(batch_start_year),'batch_label':batch_label(batch_start_year),'to_sem_no':to_sem,'targets':preview,
            'note':'Rollover updates current-state class mappings only (students/advisor/cohort/current timetable mirror). Historical records keep stored class names.'}

def apply_batch_rollover(batch_start_year, to_sem_no=None, actor='admin'):
    preview=_rollover_preview(batch_start_year, to_sem_no)
    to_sem=preview['to_sem_no']; by=int(batch_start_year)
    now=_now_iso(); changed=[]
    conn=_db(); c=conn.cursor()
    try:
        for t in preview['targets']:
            frm=t['from_class']; to=t['to_class']; ck=t['cohort_key']; sec=t['section']
            if frm==to:
                changed.append({**t,'status':'unchanged'}); continue
            c.execute("UPDATE students SET class=?, study_year=? WHERE upper(class)=upper(?)", (to, (to_sem+1)//2, frm))
            students=c.rowcount
            c.execute("UPDATE users SET advisor_class=? WHERE role IN ('staff','hod','admin') AND upper(advisor_class)=upper(?)", (to, frm))
            advisors=c.rowcount
            # keep timetable_versions historical class_name unchanged; only update cohort/current mirror and optional timetable json file copy
            src=os.path.join(TIMETABLE_DIR,f"{frm}.json"); dst=os.path.join(TIMETABLE_DIR,f"{to}.json")
            file_copied=False
            try:
                if os.path.exists(src) and not os.path.exists(dst):
                    shutil.copy2(src,dst); file_copied=True
            except Exception:
                file_copied=False
            c.execute("""INSERT INTO class_cohorts(cohort_key,batch_start_year,section,current_sem_no,current_class_name,active,created_at,updated_at)
                         VALUES (?,?,?,?,?,1,?,?)
                         ON CONFLICT(cohort_key) DO UPDATE SET current_sem_no=excluded.current_sem_no,current_class_name=excluded.current_class_name,active=1,updated_at=excluded.updated_at""",
                      (ck,by,sec,to_sem,to,now,now))
            changed.append({**t,'status':'updated','students_updated':int(students or 0),'advisor_users_updated':int(advisors or 0),'timetable_file_copied':file_copied})
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    audit_log('class_cohorts',str(by),'rollover',None,{'batch_start_year':by,'to_sem_no':to_sem,'changes':changed},details='Admin applied batch rollover')
    return {'ok':True,'batch_start_year':by,'batch_label':batch_label(by),'to_sem_no':to_sem,'changes':changed,'note':preview['note']}

init_db(); seed_defaults(); ensure_semester_seed(); seed_timetable_versions(); ensure_class_cohorts_seed(); migrate_semester_ids_best_effort()

# ==============================================================================
# AUTH / DECORATORS
# ==============================================================================
def _is_api_request():
    return request.path.startswith('/api/') or request.is_json or request.headers.get('X-Requested-With')=='XMLHttpRequest'

def _session_account_active():
    uid=session.get('user_id')
    if not uid: return True
    conn=_db(); c=conn.cursor()
    try:
        c.execute('SELECT active,role,student_id FROM users WHERE id=?',(uid,)); row=c.fetchone()
        if not row or int(row[0] or 0)!=1: return False
        sid=session.get('student_id') or row[2]
        if (session.get('role')=='student' or row[1]=='student') and sid:
            c.execute('SELECT active FROM students WHERE id=?',(sid,)); srow=c.fetchone()
            if not srow or int(srow[0] or 0)!=1: return False
        return True
    except Exception:
        return True
    finally:
        conn.close()

def _reject_inactive():
    session.clear()
    if _is_api_request():
        return jsonify({'success':False,'error':'Account is inactive. Contact the administrator.'}), 401
    return redirect(url_for('login'))

def login_required(f):
    @wraps(f)
    def w(*a,**k):
        if 'logged_in' not in session: return redirect(url_for('login'))
        if not _session_account_active(): return _reject_inactive()
        return f(*a,**k)
    return w

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a,**k):
            if 'logged_in' not in session: return redirect(url_for('login'))
            if not _session_account_active(): return _reject_inactive()
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

def _student_roles_map(student_ids):
    ids = sorted({int(x) for x in student_ids if x})
    if not ids:
        return {}
    conn = _db(); c = conn.cursor()
    _ensure_student_role_column(c)
    placeholders = ','.join('?' for _ in ids)
    rows = c.execute(f'SELECT id,student_role FROM students WHERE id IN ({placeholders})', ids).fetchall()
    conn.close()
    return {int(r[0]): r[1] or '' for r in rows}

def _student_profile_dict(student_id):
    conn=_db(); c=conn.cursor()
    for col in ['email','phone','gender','blood_group','guardian_name','guardian_phone','address']:
        _ensure_column(c,'students',col,'TEXT')
    _ensure_student_role_column(c)
    c.execute("""SELECT id,name,roll,class,section,reg_no,dob,email,phone,gender,blood_group,guardian_name,guardian_phone,address,active,student_role
                 FROM students WHERE id=?""", (student_id,))
    r=c.fetchone(); conn.close()
    if not r: return None
    keys=['id','name','roll','class','section','reg_no','dob','email','phone','gender','blood_group','guardian_name','guardian_phone','address','active','student_role']
    return dict(zip(keys, r))

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
                                    s.id,s.dob,s.reg_no,s.name,s.class,u.active,s.active
                             FROM users u LEFT JOIN students s ON s.id=u.student_id
                             WHERE u.role="student" AND (upper(u.username)=upper(?) OR upper(s.reg_no)=upper(?))''', (reg_no, reg_no))
                row = c.fetchone()
                if row:
                    if int(row[14] or 0) != 1 or int(row[15] or 0) != 1:
                        return jsonify({'success':False,'message':'This student account is inactive. Contact the administrator.'}), 403
                    stored_dob = _normalize_dob(row[10] or '')
                    if stored_dob and stored_dob == dob:
                        user = row
                    else:
                        c.execute('SELECT password FROM users WHERE id=?', (row[0],)); pw=c.fetchone()
                        if pw and pw[0] == _hash_pw(dob): user = row
                if not user:
                    c.execute('SELECT id,name,reg_no,dob,class,section,active FROM students WHERE upper(reg_no)=upper(?)', (reg_no,))
                    s = c.fetchone()
                    if s and int(s[6] if len(s)>6 and s[6] is not None else 1) != 1:
                        return jsonify({'success':False,'message':'This student account is inactive. Contact the administrator.'}), 403
                    if s and _normalize_dob(s[3]) == dob:
                        uname = _upper(s[2] or reg_no)
                        c.execute('SELECT id FROM users WHERE username=?', (uname,))
                        if not c.fetchone():
                            c.execute('INSERT INTO users (username,password,role,student_id,display_name,dob,created_at) VALUES (?,?,?,?,?,?,?)',
                                      (uname,_hash_pw(dob),'student',s[0],s[1],dob,datetime.datetime.utcnow().isoformat()))
                            conn.commit()
                        c.execute('''SELECT u.id,u.username,u.role,u.student_id,u.display_name,u.staff_code,u.is_hod,u.is_advisor,u.advisor_class,
                                            s.id,s.dob,s.reg_no,s.name,s.class,u.active,s.active
                                     FROM users u LEFT JOIN students s ON s.id=u.student_id WHERE u.username=?''', (uname,))
                        user = c.fetchone()
            else:
                user_id = _norm(data.get('user_id') or data.get('username'))
                password = data.get('password') or ''
                if not user_id or not password:
                    return jsonify({'success':False,'message':'Enter User ID and Password.'}), 400
                c.execute('''SELECT id,username,role,student_id,display_name,staff_code,is_hod,is_advisor,advisor_class,password,active
                             FROM users WHERE upper(username)=upper(?) OR upper(staff_code)=upper(?)''', (user_id, user_id))
                row = c.fetchone()
                if row and row[9] == _hash_pw(password):
                    if int(row[10] or 0) != 1:
                        return jsonify({'success':False,'message':'This account is inactive. Contact the administrator.'}), 403
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
        profile = _student_profile_dict(student[0]) or {}
        info = {"id":student[0],"name":student[1],"roll":student[2],"class":student[3],"section":student[4],"reg_no":student[5],"dob":student[7] if len(student)>7 else None,
                "student_role":profile.get('student_role','')}
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
    if request.method == 'GET': return render_template('add_student.html', classes=get_classes(), role_presets=STUDENT_ROLE_PRESETS)
    data = request.form
    name = _norm(data.get('name')); roll=_norm(data.get('roll')); cls=_norm(data.get('class')); sec=_norm(data.get('sec') or data.get('section'))
    reg_no=_upper(data.get('reg_no')); dob=_normalize_dob(data.get('dob'))
    email=_norm(data.get('email')); phone=_norm(data.get('phone')); gender=_norm(data.get('gender')); blood=_norm(data.get('blood_group'))
    guardian_name=_norm(data.get('guardian_name')); guardian_phone=_norm(data.get('guardian_phone')); address=_norm(data.get('address'))
    role_value = data.get('student_role_custom') if _norm(data.get('student_role')) == 'Other' else data.get('student_role')
    role = _normalize_student_role(role_value)
    derived_batch, derived_study = _class_year_info(cls)
    batch = _normalize_batch_year(data.get('batch_year')) or derived_batch
    study = _normalize_study_year(data.get('study_year')) or derived_study
    if _normalize_batch_year(data.get('batch_year')) is None:
        return jsonify({'error':'Batch year must be in YYYY-YYYY format.'}), 400
    if _normalize_study_year(data.get('study_year')) is None:
        return jsonify({'error':'Study year must be between 1 and 4.'}), 400
    if not name: return jsonify({'error':'name required'}), 400
    if not reg_no or not dob: return jsonify({'error':'Register Number and DOB are required (student login = Reg No + DOB).'}), 400
    if cls and cls not in get_classes(): return jsonify({'error':f'Unknown class: {cls}'}), 400
    conn=_db(); c=conn.cursor()
    for col in ['email','phone','gender','blood_group','guardian_name','guardian_phone','address']:
        _ensure_column(c,'students',col,'TEXT')
    _ensure_student_year_columns(c)
    _ensure_student_role_column(c)
    # Duplicate prevention is keyed on Register Number (roll numbers are not unique/stable).
    c.execute('SELECT id,name,class,reg_no FROM students WHERE upper(trim(COALESCE(reg_no,"")))=?', (reg_no,))
    dup=c.fetchone()
    if dup:
        conn.close()
        return jsonify({'error':f'Duplicate student: Reg No {reg_no} already exists for {dup[1]} ({dup[2] or "no class"}).', 'duplicate':True, 'student_id':dup[0], 'existing_name':dup[1]}), 409
    c.execute('SELECT id,role FROM users WHERE upper(username)=?', (reg_no,))
    u=c.fetchone()
    if u and u[1] != 'student':
        conn.close()
        return jsonify({'error':f'Login ID {reg_no} is already used by a {u[1]} account.'}), 409
    c.execute('''INSERT INTO students (name,roll,class,section,reg_no,dob,email,phone,gender,blood_group,guardian_name,guardian_phone,address,batch_year,study_year,student_role,created_at,active)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)''',
               (name,roll,cls,sec,reg_no,dob,email,phone,gender,blood,guardian_name,guardian_phone,address,batch,int(study) if study else None,role,datetime.datetime.utcnow().isoformat()))
    sid=c.lastrowid
    c.execute('SELECT id FROM users WHERE username=?',(reg_no,))
    if not c.fetchone():
        c.execute('INSERT INTO users (username,password,role,student_id,display_name,dob,email,phone,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (reg_no,_hash_pw(dob),'student',sid,name,dob,email,phone,1,datetime.datetime.utcnow().isoformat()))
    else:
        c.execute('UPDATE users SET student_id=?, display_name=?, dob=?, email=?, phone=?, active=1, password=? WHERE username=? AND role="student"', (sid,name,dob,email,phone,_hash_pw(dob),reg_no))
    conn.commit(); conn.close()
    os.makedirs(os.path.join(DATASET_DIR,str(sid)), exist_ok=True)
    audit_log('student', sid, 'add', None, {'name':name,'reg_no':reg_no,'class':cls,'roll':roll,'batch_year':batch,'study_year':study,'student_role':role}, details='Admin added student')
    return jsonify({'student_id':sid, 'reg_no':reg_no})

def _save_profile_photo(student_id, file_storage):
    if not file_storage or not file_storage.filename: return None
    ext=os.path.splitext(secure_filename(file_storage.filename))[1].lower()
    if ext not in ('.jpg','.jpeg','.png','.webp'): ext='.jpg'
    for old_ext in ('.jpg','.jpeg','.png','.webp'):
        old=os.path.join(PROFILE_PHOTOS_DIR, f'{int(student_id)}{old_ext}')
        if os.path.exists(old):
            try: os.remove(old)
            except Exception: pass
    path=os.path.join(PROFILE_PHOTOS_DIR, f'{int(student_id)}{ext}')
    file_storage.save(path)
    return path

def _save_training_images(student_id, files, replace=False):
    folder=os.path.join(DATASET_DIR,str(int(student_id))); os.makedirs(folder,exist_ok=True)
    if replace and os.path.isdir(folder):
        for fn in os.listdir(folder):
            if fn.lower().endswith(('.jpg','.jpeg','.png','.webp')):
                try: os.remove(os.path.join(folder,fn))
                except Exception: pass
    saved=0
    for f in files or []:
        if not f or not getattr(f,'filename',''): continue
        try:
            ext=os.path.splitext(secure_filename(f.filename or 'img.jpg'))[1].lower() or '.jpg'
            if ext not in ('.jpg','.jpeg','.png','.webp'): ext='.jpg'
            name=f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{saved}{ext}"
            f.save(os.path.join(folder,name)); saved+=1
        except Exception as e: app.logger.error('Save error: %s', e)
    return saved

@app.route('/upload_face', methods=['POST'])
@admin_required
def upload_face():
    student_id = request.form.get('student_id')
    if not student_id: return jsonify({'error':'student_id required'}), 400
    try: student_id = int(student_id)
    except Exception: return jsonify({'error':'Invalid student_id'}), 400
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,name,reg_no FROM students WHERE id=?', (student_id,)); st=c.fetchone(); conn.close()
    if not st: return jsonify({'error':'Student not found. Add student details first.'}), 404
    files = request.files.getlist('images[]')
    replace=str(request.form.get('replace') or '').lower() in ('1','true','yes','on')
    saved=_save_training_images(student_id, files, replace=replace)
    if not saved: return jsonify({'error':'No training images received.'}), 400
    audit_log('student', student_id, 'upload_training_images', None, {'saved':saved,'reg_no':st[2],'replace':replace}, details='Face training images uploaded')
    return jsonify({'saved':saved, 'student_id':student_id, 'reg_no':st[2], 'replace':replace})

@app.route('/api/admin/students/<int:sid>/profile_photo', methods=['POST'])
@admin_required
def api_admin_student_profile_photo(sid):
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,name FROM students WHERE id=?',(sid,)); st=c.fetchone(); conn.close()
    if not st: return jsonify({'ok':False,'error':'Student not found'}), 404
    f=request.files.get('photo')
    if not f or not f.filename: return jsonify({'ok':False,'error':'Photo file required'}), 400
    path=_save_profile_photo(sid,f)
    audit_log('student', sid, 'profile_photo', None, {'file':os.path.basename(path)}, details='Admin uploaded profile photo')
    return jsonify({'ok':True,'photo_url':f'/student_photo_safe/{sid}?t={int(datetime.datetime.utcnow().timestamp())}'})

@app.route('/api/admin/students/<int:sid>/training_images', methods=['POST'])
@admin_required
def api_admin_student_training_images(sid):
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,name,reg_no FROM students WHERE id=?',(sid,)); st=c.fetchone(); conn.close()
    if not st: return jsonify({'ok':False,'error':'Student not found'}), 404
    files=request.files.getlist('images[]') or request.files.getlist('images')
    replace=str(request.form.get('replace') or '').lower() in ('1','true','yes','on')
    saved=_save_training_images(sid, files, replace=replace)
    if not saved: return jsonify({'ok':False,'error':'No training images received'}), 400
    audit_log('student', sid, 'upload_training_images', None, {'saved':saved,'replace':replace,'reg_no':st[2]}, details='Admin uploaded training images')
    return jsonify({'ok':True,'saved':saved,'count':_student_training_count(sid),'replace':replace})

@app.route('/students', methods=['GET'])
@staff_required
def students_list():
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,roll,class,section,reg_no,dob,batch_year,study_year,student_role FROM students ORDER BY name ASC')
    rows=c.fetchall(); conn.close()
    return jsonify({'students':[{'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'dob':r[6],'batch_year':r[7] or '', 'study_year':r[8] or '', 'student_role':r[9] or ''} for r in rows]})

@app.route('/admin/students')
@admin_required
def admin_students_page():
    conn=_db(); c=conn.cursor()
    for col in ['email','phone','guardian_name','guardian_phone','address','blood_group','gender']: _ensure_column(c,'students',col,'TEXT')
    _backfill_student_years(c)
    _ensure_student_role_column(c)
    c.execute('''SELECT id,name,roll,class,section,reg_no,created_at,dob,email,phone,guardian_name,guardian_phone,
                        active,gender,batch_year,study_year,student_role
                 FROM students ORDER BY class,name,id DESC''')
    rows=c.fetchall(); conn.commit(); conn.close()
    students=[]
    for r in rows:
        img_count=_student_training_count(r[0])
        has_profile=bool(_student_profile_photo_path(r[0]))
        students.append({'id':r[0],'name':r[1],'roll':r[2] or '','class':r[3] or '','section':r[4] or '',
                         'reg_no':r[5] or '','created_at':r[6] or '','dob':r[7] or '',
                         'email':r[8] or '','phone':r[9] or '','guardian_name':r[10] or '',
                         'guardian_phone':r[11] or '','active':int(r[12] or 0),'gender':r[13] or '',
                         'batch_year':r[14] or '','study_year':int(r[15] or 0) if r[15] else '',
                         'student_role':r[16] or '','image_count':img_count,'has_profile_photo':has_profile})
    return render_template('students_list.html', students=students)

def _admin_student_attendance_data(sid, semester_id='', date_from='', date_to='', status='', period='', sem_no=None, batch_start_year=None):
    conn=_db(); c=conn.cursor(); _ensure_student_year_columns(c); _ensure_student_role_column(c)
    row=c.execute('SELECT id,name,class,section,reg_no,batch_year,study_year,gender,active,student_role FROM students WHERE id=?',(sid,)).fetchone()
    if not row: conn.close(); return None,[],{},{}
    student={'id':row[0],'name':row[1],'class':row[2] or '','section':row[3] or '','reg_no':row[4] or '','batch_year':row[5] or '','study_year':row[6] or '','gender':row[7] or '','active':int(row[8] or 0),'student_role':row[9] or ''}
    sem=_semester_by_id(semester_id) if semester_id else None
    if not sem and sem_no not in (None,'') and batch_start_year not in (None,''):
        try: sem=_batch_semester_row(int(batch_start_year), int(sem_no))
        except Exception: sem=None
    if not sem and sem_no not in (None,''):
        info=parse_cohort_class(student.get('class') or '')
        by=info.get('batch_start_year') if info else None
        try:
            if by is not None: sem=_batch_semester_row(int(by), int(sem_no))
        except Exception: sem=None
    if sem and not date_from and not date_to:
        wf,wt=_semester_window(sem,student['class']); date_from=wf.isoformat(); date_to=wt.isoformat()
    where=['e.student_id=?']; params=[sid]
    if date_from: where.append('s.date>=?'); params.append(date_from)
    if date_to: where.append('s.date<=?'); params.append(date_to)
    if status:
        stf='-' if status.strip()=='-' else _normalize_attendance_status(status)
        where.append('COALESCE(e.status,"")=?'); params.append(stf)
    if period:
        try: where.append('s.period=?'); params.append(int(period))
        except Exception: period=''
    rows=c.execute("SELECT s.date,s.period,s.subject_display,s.class_name,e.status,e.marked_by,e.source,e.updated_at,s.submitted_at,COALESCE(NULLIF(e.elective_label,''),s.subject_display,s.subject) FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id WHERE "+' AND '.join(where)+" ORDER BY s.date DESC,s.period DESC,e.updated_at DESC LIMIT 2000",params).fetchall(); conn.close()
    details=[]; total=credited=absent=late=od=medical=unmarked=0
    for r in rows:
        st=_normalize_attendance_status(r[4]); sk=_analytics_status_key(st)
        if sk=='unmarked': unmarked+=1
        else:
            total+=1
            if sk in ('present','late','od','medical'): credited+=1
            if sk=='absent': absent+=1
            if sk=='late': late+=1
            if sk=='od': od+=1
            if sk=='medical': medical+=1
        details.append({'date':r[0] or '','period':r[1] or '','subject':r[9] or r[2] or '','class':r[3] or '','status':st,'status_key':sk,'marked_by':r[5] or '','source':r[6] or '','updated_at':r[7] or '','submitted_at':r[8] or ''})
    summary={'total':total,'credited':credited,'absent':absent,'late':late,'od':od,'medical':medical,'unmarked':unmarked,'percentage':round((credited/total*100) if total else 0,2)}
    filters={'semester_id':semester_id,'date_from':date_from,'date_to':date_to,'status':_normalize_attendance_status(status) if status else '','period':str(period) if period else ''}
    return student,details,summary,filters

@app.route('/admin/students/<int:sid>/attendance')
@admin_required
def admin_student_attendance(sid):
    semester_id=_norm(request.args.get('semester_id')); date_from=_norm(request.args.get('from')); date_to=_norm(request.args.get('to')); status=_norm(request.args.get('status')); period=_norm(request.args.get('period'))
    sem_no=_norm(request.args.get('sem_no')); batch_start_year=_norm(request.args.get('batch_start_year'))
    student,details,summary,filters=_admin_student_attendance_data(sid,semester_id,date_from,date_to,status,period,sem_no=sem_no,batch_start_year=batch_start_year)
    if not student: abort(404)
    cls=student.get('class') or ''
    info=parse_cohort_class(cls); by=info.get('batch_start_year') if info else None
    sem_rows=_semester_rows(batch_start_year=by, include_legacy=by is None) if by else _semester_rows()
    cur_no=current_sem_for_batch(by) if by else None
    sem_opts=[_semester_json(s,batch_start_year=by,on_date=_today_ist_str()) for s in sem_rows if not by or s.get('batch_start_year')==by]
    return render_template('admin_student_attendance.html', student=student, details=details, summary=summary,
                           semesters=sem_opts, semester_id=filters['semester_id'], current_sem_no=cur_no, batch_start_year=by,
                           date_from=filters['date_from'], date_to=filters['date_to'], status=filters['status'], period=filters['period'])

@app.route('/admin/students/<int:sid>/attendance/export')
@admin_required
def admin_student_attendance_export(sid):
    semester_id=_norm(request.args.get('semester_id')); date_from=_norm(request.args.get('from')); date_to=_norm(request.args.get('to')); status=_norm(request.args.get('status')); period=_norm(request.args.get('period'))
    sem_no=_norm(request.args.get('sem_no')); batch_start_year=_norm(request.args.get('batch_start_year'))
    student,details,_summary,_filters=_admin_student_attendance_data(sid,semester_id,date_from,date_to,status,period,sem_no=sem_no,batch_start_year=batch_start_year)
    if not student: abort(404)
    output=io.StringIO(); output.write('Date,Period,Class,Subject,Status,Source,MarkedBy,Updated,Submitted\n')
    for d in details:
        output.write(','.join('"'+str(x or '').replace('"','""')+'"' for x in [d['date'],d['period'],d['class'],d['subject'],d['status'],d['source'],d['marked_by'],d['updated_at'],d['submitted_at']])+'\n')
    mem=io.BytesIO(output.getvalue().encode('utf-8-sig')); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f"student_{student.get('reg_no') or sid}_attendance.csv", mimetype='text/csv')


# --- Admin: staff / HOD / advisor management ---
@app.route('/admin/staff')
@admin_required
def admin_staff_page():
    return render_template('admin_staff.html', classes=get_classes())

STAFF_PROFILE_FIELDS = ('dob','gender','designation','department','qualification','address','date_of_joining','phone','email')

def _bool_flag(value, default=False):
    if value is None or value == '': return 1 if default else 0
    return 1 if str(value).strip().lower() in ('1','true','on','yes','y') else 0

def _staff_row_dict(row):
    if row is None: return None
    keys=['id','username','display_name','staff_code','role','is_hod','is_advisor','advisor_class','dob','gender','designation','department','qualification','address','date_of_joining','phone','email','active','deleted_at']
    return dict(zip(keys,row))

def _staff_profile_payload(data, code, display, advisor_class, is_hod, active):
    return {
        'username':code, 'staff_code':code, 'display_name':display, 'role':'staff',
        'is_hod':is_hod, 'is_advisor':1 if advisor_class else 0, 'advisor_class':advisor_class or None,
        'dob':_norm(data.get('dob')) or None, 'gender':_norm(data.get('gender')) or None,
        'designation':_norm(data.get('designation')) or None, 'department':_norm(data.get('department')) or None,
        'qualification':_norm(data.get('qualification')) or None, 'address':_norm(data.get('address')) or None,
        'date_of_joining':_norm(data.get('date_of_joining')) or None,
        'phone':_norm(data.get('phone')) or None, 'email':_norm(data.get('email')) or None,
        'active':active,
    }

def _staff_rows():
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT id,username,display_name,staff_code,role,is_hod,is_advisor,advisor_class,dob,gender,designation,department,qualification,address,date_of_joining,phone,email,active,deleted_at
                 FROM users WHERE role IN ('staff','hod','admin') AND username!='ADMIN' ORDER BY display_name COLLATE NOCASE""")
    rows=c.fetchall(); conn.close()
    return [_staff_row_dict(r) for r in rows]

@app.route('/api/admin/staff', methods=['GET','POST'])
@admin_required
def api_admin_staff():
    if request.method=='GET':
        tt_staff, advisors = all_timetable_staff()
        return jsonify({'ok':True,'staff':_staff_rows(),'timetable_staff':tt_staff,'advisors':advisors})
    data=request.get_json(silent=True) or request.form.to_dict()
    code=_upper(data.get('staff_code') or data.get('username') or data.get('code'))
    display=_norm(data.get('display_name') or data.get('name'))
    if not code or not display: return jsonify({'ok':False,'error':'Staff code and display name are required.'}), 400
    if not re.fullmatch(r'[A-Z0-9._-]{1,20}', code): return jsonify({'ok':False,'error':'Staff code must be 1-20 letters/digits/._-'}), 400
    is_hod=1 if _bool_flag(data.get('is_hod')) or code==HOD_STAFF_CODE else 0
    advisor_class=_norm(data.get('advisor_class'))
    if advisor_class and advisor_class not in get_classes(): return jsonify({'ok':False,'error':'Unknown advisor class.'}), 400
    active=_bool_flag(data.get('active', 1), True)
    password=(data.get('password') or '').strip()
    profile=_staff_profile_payload(data, code, display, advisor_class, is_hod, active)
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT id,username,display_name,staff_code,role,is_hod,is_advisor,advisor_class,dob,gender,designation,department,qualification,address,date_of_joining,phone,email,active,deleted_at
                 FROM users WHERE upper(username)=?""", (code,)); existing_row=c.fetchone()
    if existing_row and existing_row[4]=='student':
        conn.close(); return jsonify({'ok':False,'error':'This code is already used by a student login.'}), 409
    now=_now_iso()
    if existing_row:
        old=_staff_row_dict(existing_row)
        deleted_at=old.get('deleted_at') if not active else None
        c.execute("""UPDATE users SET username=?,staff_code=?,display_name=?,role='staff',is_hod=?,is_advisor=?,advisor_class=?,dob=?,gender=?,designation=?,department=?,qualification=?,address=?,date_of_joining=?,phone=?,email=?,active=?,deleted_at=? WHERE id=?""",
                  (code, code, display, is_hod, 1 if advisor_class else 0, advisor_class or None,
                   profile['dob'], profile['gender'], profile['designation'], profile['department'], profile['qualification'],
                   profile['address'], profile['date_of_joining'], profile['phone'], profile['email'], active, deleted_at, existing_row[0]))
        uid=existing_row[0]; action='update'
        if password: c.execute('UPDATE users SET password=? WHERE id=?', (_hash_pw(password), uid))
    else:
        c.execute("""INSERT INTO users(username,password,role,staff_code,display_name,is_hod,is_advisor,advisor_class,dob,gender,designation,department,qualification,address,date_of_joining,phone,email,active,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (code, _hash_pw(password or _staff_password(display)), 'staff', code, display, is_hod, 1 if advisor_class else 0, advisor_class or None,
                   profile['dob'], profile['gender'], profile['designation'], profile['department'], profile['qualification'],
                   profile['address'], profile['date_of_joining'], profile['phone'], profile['email'], active, now))
        uid=c.lastrowid; action='add'; old=None
    if is_hod: c.execute('UPDATE users SET is_hod=0 WHERE is_hod=1 AND id!=?', (uid,))
    conn.commit(); conn.close()
    new={k:v for k,v in profile.items() if k!='username'}
    audit_log('staff', uid, action, old, new, details='Admin managed staff account')
    return jsonify({'ok':True,'id':uid})

@app.route('/api/admin/staff/<int:uid>/reset_password', methods=['POST'])
@admin_required
def api_admin_staff_reset(uid):
    data=request.get_json(silent=True) or {}
    conn=_db(); c=conn.cursor(); c.execute('SELECT display_name,staff_code FROM users WHERE id=?',(uid,)); r=c.fetchone()
    if not r: conn.close(); return jsonify({'ok':False,'error':'Staff not found'}), 404
    pw=(data.get('password') or '').strip() or _staff_password(r[0] or r[1])
    c.execute('UPDATE users SET password=? WHERE id=?', (_hash_pw(pw),uid)); conn.commit(); conn.close()
    audit_log('staff', uid, 'reset_password', None, {'password_reset':True}, details='Admin reset staff password')
    return jsonify({'ok':True,'password':pw})

def _set_staff_status(uid, active, _deleted, action, details):
    if session.get('user_id') == uid:
        return jsonify({'ok':False,'error':'You cannot change your own account status.'}), 400
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT id,username,display_name,staff_code,role,is_hod,is_advisor,advisor_class,dob,gender,designation,department,qualification,address,date_of_joining,phone,email,active,deleted_at
                 FROM users WHERE id=? AND role IN ('staff','hod','admin') AND username!='ADMIN'""",(uid,))
    r=c.fetchone()
    if not r: conn.close(); return jsonify({'ok':False,'error':'Staff not found'}), 404
    old=_staff_row_dict(r)
    c.execute('UPDATE users SET active=?,deleted_at=NULL WHERE id=?',(1 if active else 0, uid))
    conn.commit(); conn.close()
    audit_log('staff', uid, action, {'active':old.get('active')}, {'active':1 if active else 0}, details=details)
    return jsonify({'ok':True,'active':1 if active else 0})

@app.route('/api/admin/staff/<int:uid>/deactivate', methods=['POST'])
@admin_required
def api_admin_staff_deactivate(uid):
    return _set_staff_status(uid, False, False, 'deactivate', 'Admin deactivated staff account')

@app.route('/api/admin/staff/<int:uid>/reactivate', methods=['POST'])
@admin_required
def api_admin_staff_reactivate(uid):
    return _set_staff_status(uid, True, False, 'reactivate', 'Admin activated staff account')

@app.route('/api/admin/staff/<int:uid>/toggle', methods=['POST'])
@admin_required
def api_admin_staff_toggle(uid):
    conn=_db(); c=conn.cursor(); c.execute('SELECT active FROM users WHERE id=? AND role IN ("staff","hod","admin") AND username!="ADMIN"',(uid,)); r=c.fetchone(); conn.close()
    if not r: return jsonify({'ok':False,'error':'Staff not found'}), 404
    if int(r[0] or 0)==1: return _set_staff_status(uid, False, False, 'deactivate', 'Admin deactivated staff account')
    return _set_staff_status(uid, True, False, 'reactivate', 'Admin activated staff account')

@app.route('/api/admin/staff/sync', methods=['POST'])
@admin_required
def api_admin_staff_sync():
    result=sync_staff_from_timetables(reset_passwords=False)
    audit_log('staff','timetable','sync',None,result,details='Admin synced staff from timetable files')
    return jsonify({'ok':True, **result})

@app.route('/api/admin/staff/import', methods=['POST'])
@admin_required
def api_admin_staff_import():
    f=request.files.get('file')
    if not f: return jsonify({'ok':False,'error':'CSV file required.'}), 400
    try:
        text=f.stream.read().decode('utf-8-sig')
        reader=csv.DictReader(io.StringIO(text))
        rows=list(reader)
    except Exception as e:
        return jsonify({'ok':False,'error':f'Could not parse CSV: {e}'}), 400
    added=updated=0; errors=[]
    for i,row in enumerate(rows, start=2):
        code=_upper(row.get('staff_code') or row.get('code') or row.get('username'))
        display=_norm(row.get('display_name') or row.get('name'))
        if not code or not display:
            errors.append(f'Row {i}: staff_code and display_name required'); continue
        conn=_db(); c=conn.cursor(); c.execute('SELECT id,role FROM users WHERE upper(username)=?',(code,)); ex=c.fetchone()
        if ex and ex[1]=='student':
            errors.append(f'Row {i}: {code} is a student login'); conn.close(); continue
        is_hod=1 if _bool_flag(row.get('is_hod')) else 0
        adv=_norm(row.get('advisor_class')); active=_bool_flag(row.get('active',1),True)
        pw=(row.get('password') or '').strip()
        profile=_staff_profile_payload(row, code, display, adv, is_hod, active)
        if ex:
            c.execute("""UPDATE users SET display_name=?,staff_code=?,role='staff',is_hod=?,is_advisor=?,advisor_class=?,dob=?,gender=?,designation=?,department=?,qualification=?,address=?,date_of_joining=?,phone=?,email=?,active=?,deleted_at=NULL WHERE id=?""",
                      (display, code, is_hod, 1 if adv else 0, adv or None, profile['dob'], profile['gender'], profile['designation'], profile['department'], profile['qualification'], profile['address'], profile['date_of_joining'], profile['phone'], profile['email'], active, ex[0]))
            if pw: c.execute('UPDATE users SET password=? WHERE id=?',(_hash_pw(pw),ex[0]))
            updated+=1
        else:
            c.execute("""INSERT INTO users(username,password,role,staff_code,display_name,is_hod,is_advisor,advisor_class,dob,gender,designation,department,qualification,address,date_of_joining,phone,email,active,created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (code,_hash_pw(pw or _staff_password(display)),'staff',code,display,is_hod,1 if adv else 0,adv or None,profile['dob'],profile['gender'],profile['designation'],profile['department'],profile['qualification'],profile['address'],profile['date_of_joining'],profile['phone'],profile['email'],active,_now_iso()))
            added+=1
        conn.commit(); conn.close()
    audit_log('staff','csv','import',None,{'added':added,'updated':updated,'errors':len(errors)},details='Admin imported staff CSV')
    return jsonify({'ok':True,'added':added,'updated':updated,'errors':errors})

# --- Admin: timetable JSON editor ---
DAY_KEYS = ('MON','TUE','WED','THU','FRI','SAT')

def _validate_timetable(data, class_name=None):
    errors=[]; warnings=[]
    if not isinstance(data, dict): return ['Timetable must be a JSON object.'], []
    cls=_norm(data.get('class') or class_name or '')
    if not cls: errors.append('class is required.')
    if class_name and cls and cls != class_name: errors.append(f'class inside JSON ({cls}) does not match filename ({class_name}).')
    adv=data.get('class_advisor') or {}
    if not isinstance(adv, dict) or not _norm(adv.get('initials')):
        errors.append('class_advisor.initials is required.')
    elif not re.fullmatch(r'[A-Za-z0-9._-]{1,20}', _norm(adv.get('initials'))):
        errors.append('class_advisor.initials has invalid characters.')
    slots=data.get('slots')
    if not isinstance(slots, list) or not slots: errors.append('slots must be a non-empty list.')
    else:
        seen=set()
        for s in slots:
            if not isinstance(s, dict): errors.append('Each slot must be an object.'); continue
            try: p=int(s.get('period'))
            except Exception: errors.append('Slot period must be an integer.'); continue
            if p in seen: errors.append(f'Duplicate slot period {p}.')
            seen.add(p)
            if not s.get('start') or not s.get('end'): warnings.append(f'Slot {p} is missing start/end time.')
        if seen and seen != set(range(1, max(seen)+1)): warnings.append('Slot periods are not contiguous starting at 1.')
    staff_dir=data.get('staff_directory') or {}
    if not isinstance(staff_dir, dict) or not staff_dir: errors.append('staff_directory must be a non-empty object.')
    known={str(k).upper() for k in staff_dir.keys()} if isinstance(staff_dir,dict) else set()
    sched=data.get('schedule')
    if not isinstance(sched, dict) or not sched: errors.append('schedule must be a non-empty object.')
    else:
        for day, cells in sched.items():
            if str(day).upper() not in DAY_KEYS: errors.append(f'Unknown schedule day: {day}'); continue
            if not isinstance(cells, list): errors.append(f'{day} must be a list of period cells.'); continue
            seen_p=set()
            for cell in cells:
                if not isinstance(cell, dict): errors.append(f'{day}: each cell must be an object.'); continue
                try: p=int(cell.get('period'))
                except Exception: errors.append(f'{day}: cell period must be an integer.'); continue
                if p in seen_p: errors.append(f'{day}: duplicate period {p}.')
                seen_p.add(p)
                if not _norm(cell.get('subject')): errors.append(f'{day} P{p}: subject is required.')
                staff=cell.get('staff') or []
                if not isinstance(staff, list): errors.append(f'{day} P{p}: staff must be a list.')
                else:
                    for code in staff:
                        if str(code).upper() not in known: warnings.append(f'{day} P{p}: staff code {code} is not in staff_directory.')
                options=cell.get('options')
                if options is not None and (not isinstance(options,list) or not options):
                    errors.append(f'{day} P{p}: options must be a non-empty list when present.')
    if errors: return errors, warnings
    if not data.get('class'): data['class']=cls
    if not data.get('staff_directory'): data['staff_directory']={}
    return [], warnings

def _timetable_path(class_name):
    cls=canonical_class_name(class_name)
    if not cls or not is_valid_class_name(cls): return None
    return os.path.join(TIMETABLE_DIR, f'{cls}.json')

def _timetable_version_status(row, today=None):
    today=today or datetime.date.today().isoformat()
    ef=(row.get('effective_from') or '').strip()
    et=(row.get('effective_to') or '').strip()
    if ef and ef>today: return 'future'
    if et and et<today: return 'past'
    return 'current'

def _timetable_version_json(row, include_data=False):
    out={
        'id':row.get('id'),'class':row.get('class_name'),'semester_id':row.get('semester_id'),
        'version_no':row.get('version_no'),'effective_from':row.get('effective_from') or '',
        'effective_to':row.get('effective_to') or '','is_active':int(row.get('is_active') or 0),
        'created_by':row.get('created_by') or '','created_at':row.get('created_at') or '',
        'updated_at':row.get('updated_at') or '','notes':row.get('notes') or '',
        'reference_path':row.get('reference_path') or '',
        'reference_name':row.get('reference_name') or '',
        'reference_url':f"/admin/timetable/reference/{row.get('id')}" if row.get('reference_path') else '',
        'hidden':int(row.get('hidden') or 0),'hidden_at':row.get('hidden_at') or '','hidden_by':row.get('hidden_by') or '',
        'status':_timetable_version_status(row)
    }
    if include_data:
        try: out['data']=json.loads(row.get('timetable_json') or '{}')
        except Exception: out['data']={}
    return out

def _timetable_version_rows(class_name=None, semester_id=None, include_hidden=False):
    conn=_db(); c=conn.cursor(); rows=[]
    try:
        q="""SELECT id,class_name,semester_id,version_no,effective_from,effective_to,timetable_json,
                    reference_path,reference_name,is_active,created_by,created_at,updated_at,notes,hidden,hidden_at,hidden_by
             FROM timetable_versions WHERE 1=1"""
        params=[]
        if class_name:
            q+=" AND upper(class_name)=upper(?)"; params.append(_norm(class_name))
        if semester_id is not None:
            q+=" AND semester_id=?"; params.append(int(semester_id))
        if not include_hidden:
            q+=" AND COALESCE(hidden,0)=0"
        q+=" ORDER BY effective_from DESC, version_no DESC, id DESC"
        c.execute(q,params); rows=c.fetchall()
    except Exception:
        rows=[]
    finally:
        conn.close()
    keys=['id','class_name','semester_id','version_no','effective_from','effective_to','timetable_json',
          'reference_path','reference_name','is_active','created_by','created_at','updated_at','notes','hidden','hidden_at','hidden_by']
    return [dict(zip(keys,r)) for r in rows]

def _write_timetable_json_file(class_name, payload):
    path=_timetable_path(class_name)
    if not path: raise ValueError('Invalid class name.')
    if os.path.exists(path):
        ts=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(path, os.path.join(TIMETABLE_BACKUP_DIR, f'{class_name}_{ts}.json'))
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp,path)

def _save_timetable_version(class_name, semester_id, effective_from, payload,
                            reference_path='', reference_name='', notes='', force_new=False):
    """Create or update a timetable version, then mirror the latest JSON file."""
    errors,warnings=_validate_timetable(payload, class_name)
    if errors:
        return {'ok':False,'error':'Validation failed','errors':errors,'warnings':warnings}
    cls=canonical_class_name(class_name)
    sem=_semester_by_id(semester_id) if semester_id else _current_semester()
    if not sem:
        return {'ok':False,'error':'A valid semester is required.'}
    semester_id=int(sem['id'])
    try:
        eff_d=datetime.date.fromisoformat((effective_from or '').strip())
    except Exception:
        return {'ok':False,'error':'Invalid effective date.'}
    eff=eff_d.isoformat()
    sem_end=(sem.get('end_date') or '').strip()
    now=datetime.datetime.utcnow().isoformat()
    who=session.get('display_name') or session.get('username') or 'admin'
    ref_path=(reference_path or '').strip().replace('\\','/')
    ref_name=(reference_name or '').strip()
    if ref_path:
        ref_abs=os.path.abspath(os.path.join(APP_DIR,ref_path))
        base=os.path.abspath(TIMETABLE_REF_DIR)
        if not (ref_abs==base or ref_abs.startswith(base+os.sep)):
            return {'ok':False,'error':'Invalid timetable reference path.'}
        ref_path=os.path.relpath(ref_abs,APP_DIR).replace('\\','/')
    conn=_db(); c=conn.cursor()
    try:
        c.execute("""SELECT id,version_no,effective_from,effective_to
                     FROM timetable_versions WHERE upper(class_name)=upper(?) AND semester_id=?
                       AND effective_from<=?
                     ORDER BY effective_from DESC, version_no DESC LIMIT 1""",(cls,semester_id,eff))
        latest=c.fetchone()
        action='create' if force_new else ('update' if latest and eff==str(latest[2] or '') else 'create')
        if action=='update':
            version_id=latest[0]
            c.execute("""UPDATE timetable_versions
                         SET timetable_json=?, reference_path=COALESCE(NULLIF(?,''),reference_path),
                             reference_name=COALESCE(NULLIF(?,''),reference_name), notes=?,
                             is_active=1, updated_at=?
                         WHERE id=?""",
                      (json.dumps(payload,ensure_ascii=False),ref_path,ref_name,(notes or '').strip(),now,version_id))
            c.execute("UPDATE timetable_versions SET is_active=0 WHERE upper(class_name)=upper(?) AND semester_id=? AND id<>?",
                      (cls,semester_id,version_id))
            c.execute("UPDATE timetable_versions SET is_active=1 WHERE id=?",(version_id,))
            version_no=int(latest[1] or 1)
            effective_to=(latest[3] or '').strip() or sem_end or None
        else:
            c.execute("""SELECT COALESCE(MAX(version_no),0) FROM timetable_versions
                         WHERE upper(class_name)=upper(?) AND semester_id=?""",(cls,semester_id))
            version_no=int(c.fetchone()[0] or 0)+1
            if latest:
                prev_day=(eff_d-datetime.timedelta(days=1)).isoformat()
                c.execute("UPDATE timetable_versions SET effective_to=?,is_active=0 WHERE id=?",(prev_day,latest[0]))
            c.execute("""UPDATE timetable_versions SET effective_to=?,is_active=0
                         WHERE upper(class_name)=upper(?) AND semester_id=? AND effective_from<?
                           AND (effective_to IS NULL OR effective_to='' OR effective_to>=?)""",
                      ((eff_d-datetime.timedelta(days=1)).isoformat(),cls,semester_id,eff,eff))
            c.execute("""SELECT effective_from FROM timetable_versions
                         WHERE upper(class_name)=upper(?) AND semester_id=? AND effective_from>?
                         ORDER BY effective_from ASC LIMIT 1""",(cls,semester_id,eff))
            nxt=c.fetchone()
            if nxt and nxt[0]:
                try: effective_to=(datetime.date.fromisoformat(nxt[0])-datetime.timedelta(days=1)).isoformat()
                except Exception: effective_to=sem_end or None
            else:
                effective_to=sem_end or None
            c.execute("UPDATE timetable_versions SET is_active=0 WHERE upper(class_name)=upper(?) AND semester_id=?",
                      (cls,semester_id))
            c.execute("""INSERT INTO timetable_versions
                         (class_name,semester_id,version_no,effective_from,effective_to,timetable_json,
                          reference_path,reference_name,is_active,created_by,created_at,updated_at,notes)
                         VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                      (class_name,semester_id,version_no,eff,effective_to,json.dumps(payload,ensure_ascii=False),
                       ref_path or None,ref_name or None,who,now,now,(notes or '').strip()))
            version_id=c.lastrowid
        _write_timetable_json_file(class_name,payload)
        conn.commit()
        return {'ok':True,'action':action,'version_id':version_id,'version_no':version_no,
                'effective_from':eff,'effective_to':effective_to or '','warnings':warnings}
    except Exception as e:
        conn.rollback()
        return {'ok':False,'error':f'Could not save timetable version: {e}'}
    finally:
        conn.close()

def _resolve_batch_sem_from_request(data=None, args=None):
    data=data or {}; args=args or request.args
    sem_raw=(data.get('semester_id') if 'semester_id' in data else args.get('semester_id'))
    sem_no_raw=(data.get('sem_no') if 'sem_no' in data else args.get('sem_no'))
    by_raw=(data.get('batch_start_year') if 'batch_start_year' in data else args.get('batch_start_year'))
    class_name=_norm((data.get('class') if 'class' in data else args.get('class')) or (data.get('class_name') if 'class_name' in data else args.get('class_name')))
    by=None
    if by_raw not in (None,''):
        try: by=int(by_raw)
        except Exception: by=None
    if by is None and class_name:
        info=parse_cohort_class(class_name)
        by=info.get('batch_start_year') if info else None
    sem=None
    if sem_no_raw not in (None,'') and by is not None:
        try: sem=get_or_create_batch_semester(by,int(sem_no_raw),actor=session.get('username') or 'admin')
        except Exception: sem=None
    elif sem_raw not in (None,''):
        sem=_semester_by_id(sem_raw)
        if sem and by is None: by=sem.get('batch_start_year')
    return sem, by

@app.route('/admin/timetable')
@admin_required
def admin_timetable_page():
    batches=[{'batch_start_year':by,'label':batch_label(by)} for by in BATCH_START_YEARS]
    return render_template('admin_timetable.html', classes=get_classes(), batches=batches, today=_today_ist_str())

@app.route('/api/admin/timetables')
@admin_required
def api_admin_timetables():
    out=[]
    for cls in get_classes():
        path=_timetable_path(cls)
        try:
            with open(path,'r',encoding='utf-8-sig') as f: text=f.read()
            data=json.loads(text)
            out.append({'class':cls,'hall':data.get('hall',''),'advisor':(data.get('class_advisor') or {}).get('name',''),'text':text})
        except Exception as e:
            out.append({'class':cls,'error':str(e),'text':''})
    return jsonify({'ok':True,'timetables':out,'classes':get_classes()})

@app.route('/api/admin/timetable/<class_name>', methods=['GET','POST'])
@admin_required
def api_admin_timetable(class_name):
    path=_timetable_path(class_name)
    if not path: return jsonify({'ok':False,'error':'Invalid class name.'}), 400
    if request.method=='GET':
        if not os.path.exists(path): return jsonify({'ok':False,'error':'Timetable not found.'}), 404
        with open(path,'r',encoding='utf-8-sig') as f: text=f.read()
        return jsonify({'ok':True,'class':class_name,'text':text,'data':json.loads(text)})
    data=request.get_json(silent=True) or {}
    raw=data.get('json_text')
    if raw is None:
        payload=data.get('data') or data
        raw=json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        payload=json.loads(raw) if isinstance(raw,str) else raw
    except Exception as e:
        return jsonify({'ok':False,'error':f'Invalid JSON: {e}'}), 400
    errors, warnings=_validate_timetable(payload, class_name)
    if errors: return jsonify({'ok':False,'error':'Validation failed','errors':errors,'warnings':warnings}), 400
    if os.path.exists(path):
        ts=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(path, os.path.join(TIMETABLE_BACKUP_DIR, f'{class_name}_{ts}.json'))
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: f.write(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp,path)
    sync_result=sync_staff_from_timetables(reset_passwords=False)
    audit_log('timetable', class_name, 'update', None, {'classes':get_classes(),'staff_sync':sync_result}, details='Admin saved timetable JSON after validation')
    return jsonify({'ok':True,'warnings':warnings,'staff_sync':sync_result})

@app.route('/api/admin/timetable/<class_name>/validate', methods=['POST'])
@admin_required
def api_admin_timetable_validate(class_name):
    data=request.get_json(silent=True) or {}
    raw=data.get('json_text')
    try: payload=json.loads(raw) if isinstance(raw,str) else (data.get('data') or data)
    except Exception as e: return jsonify({'ok':False,'error':f'Invalid JSON: {e}'}), 400
    errors, warnings=_validate_timetable(payload, class_name)
    return jsonify({'ok':not errors,'errors':errors,'warnings':warnings})

@app.route('/api/admin/timetable/<class_name>/create', methods=['POST'])
@admin_required
def api_admin_timetable_create(class_name):
    cls=canonical_class_name(class_name)
    if not is_valid_class_name(cls):
        return jsonify({'ok':False,'error':'Invalid class name. Use YYIT<sem><section> (example: 24IT5A).'}), 400
    path=_timetable_path(cls)
    if not path: return jsonify({'ok':False,'error':'Invalid class name.'}), 400
    if os.path.exists(path): return jsonify({'ok':False,'error':'Timetable already exists.'}), 409
    template={
        'class':cls,'hall':'','class_advisor':{'name':'','initials':'','is_hod':False},
        'slots':[{'period':i,'label':f'Hour {i}','start':'','end':''} for i in range(1,8)],
        'breaks':[],'lunch':{'start':'','end':''},
        'schedule':{d:[] for d in DAY_KEYS},
        'staff_directory':{}
    }
    with open(path,'w',encoding='utf-8') as f: f.write(json.dumps(template,ensure_ascii=False,indent=2))
    audit_log('timetable', cls, 'create', None, {'template':True}, details='Admin created timetable template')
    return jsonify({'ok':True,'class':cls,'text':json.dumps(template,ensure_ascii=False,indent=2)})

@app.route('/api/admin/timetable/versions', methods=['GET'])
@admin_required
def api_admin_timetable_versions():
    cls=_norm(request.args.get('class') or '')
    sem, by=_resolve_batch_sem_from_request()
    sem_id=int(sem['id']) if sem and sem.get('id') else None
    rows=_timetable_version_rows(cls or None,sem_id,include_hidden=True)
    if cls and sem_id is not None and not rows:
        info=parse_cohort_class(cls)
        try:
            conn=_db(); c=conn.cursor()
            if info and info.get('batch_start_year')==by and int(info.get('sem_no') or 0)==int((sem or {}).get('sem_no') or 0):
                c.execute("UPDATE timetable_versions SET semester_id=? WHERE upper(class_name)=upper(?) AND (semester_id IS NULL OR semester_id<>?)", (sem_id, cls, sem_id))
            else:
                c.execute("SELECT id,semester_id,effective_from FROM timetable_versions WHERE upper(class_name)=upper(?)", (cls,))
                for vid, old_sid, eff in c.fetchall():
                    old=_semester_by_id(old_sid) if old_sid else None
                    target_sem_no=int((sem or {}).get('sem_no') or 0) or int(current_sem_for_batch(by,on_date=eff or _today_ist_str()))
                    target=get_or_create_batch_semester(by,target_sem_no,actor='timetable_repair')
                    if target and target.get('id') and int(target['id'])!=int(old_sid or 0) and (not old or old.get('batch_start_year')!=by):
                        c.execute("UPDATE timetable_versions SET semester_id=? WHERE id=?", (int(target['id']), vid))
            conn.commit(); conn.close()
        except Exception:
            try: conn.close()
            except Exception: pass
        rows=_timetable_version_rows(cls or None,sem_id,include_hidden=True)
    versions=[_timetable_version_json(r) for r in rows]
    return jsonify({'ok':True,'class':cls,'batch_start_year':by,'semester_id':sem_id,'sem_no':(sem or {}).get('sem_no'),'versions':versions})

@app.route('/api/admin/timetable/versions/<int:version_id>', methods=['GET'])
@admin_required
def api_admin_timetable_version_detail(version_id):
    conn=_db(); c=conn.cursor()
    try:
        c.execute("""SELECT id,class_name,semester_id,version_no,effective_from,effective_to,timetable_json,
                            reference_path,reference_name,is_active,created_by,created_at,updated_at,notes
                     FROM timetable_versions WHERE id=?""",(version_id,))
        row=c.fetchone()
    except Exception:
        row=None
    finally:
        conn.close()
    if not row: return jsonify({'ok':False,'error':'Timetable version not found.'}), 404
    keys=['id','class_name','semester_id','version_no','effective_from','effective_to','timetable_json',
          'reference_path','reference_name','is_active','created_by','created_at','updated_at','notes']
    out=_timetable_version_json(dict(zip(keys,row)),include_data=True)
    return jsonify({'ok':True,'version':out})

@app.route('/api/admin/timetable/import/preview', methods=['POST'])
@admin_required
def api_admin_timetable_import_preview():
    try:
        data=request.get_json(silent=True) or {}
        text=data.get('text') or ''
        cls=canonical_class_name(data.get('class') or '')
        sem, _by=_resolve_batch_sem_from_request(data=data)
        sem_id=int(sem['id']) if sem and sem.get('id') else None
        res=_parse_timetable_import(text, cls, sem_id)
        return jsonify(res), (200 if res.get('ok') else 400)
    except Exception as e:
        app.logger.exception('Timetable import preview failed')
        return jsonify({'ok':False,'errors':[f'Import preview failed: {str(e)}'],'warnings':[]}), 500

@app.route('/api/admin/timetable/import/apply', methods=['POST'])
@admin_required
def api_admin_timetable_import_apply():
    try:
        data=request.get_json(silent=True) or {}
        text=data.get('text') or ''
        cls=canonical_class_name(data.get('class') or '')
        sem, _by=_resolve_batch_sem_from_request(data=data)
        sem_id=int(sem['id']) if sem else None
        allowed, reason=_sem_edit_allowed(sem)
        if not allowed: return jsonify({'ok':False,'error':reason}), 403
        effective_from=(data.get('effective_from') or _today_ist_str()).strip()
        preview=_parse_timetable_import(text, cls, sem_id)
        if not preview.get('ok'):
            return jsonify(preview), 400
        payload=preview.get('data') or {}
        cls=canonical_class_name(payload.get('class') or cls)
        res=_save_timetable_version(cls, sem_id, effective_from, payload, notes=(data.get('notes') or 'Imported timetable'), force_new=True)
        if not res.get('ok'):
            return jsonify(res), 400
        staff_sync=sync_staff_from_timetables(reset_passwords=False)
        audit_log('timetable', cls, 'import', None, {'version_id':res.get('version_id'),'effective_from':effective_from}, details='Admin imported timetable')
        res['staff_sync']=staff_sync
        res['import']= {'warnings':preview.get('warnings',[]),'staff_count':preview.get('staff_count',0)}
        return jsonify(res)
    except Exception as e:
        app.logger.exception('Timetable import apply failed')
        return jsonify({'ok':False,'errors':[f'Import failed: {str(e)}'],'warnings':[]}), 500

@app.route('/api/admin/timetable/versions', methods=['POST'])
@admin_required
def api_admin_timetable_version_create():
    data=request.get_json(silent=True) or {}
    cls=canonical_class_name(data.get('class') or '')
    if not cls: return jsonify({'ok':False,'error':'Class is required.'}), 400
    if not is_valid_class_name(cls): return jsonify({'ok':False,'error':'Invalid class name. Use YYIT<sem><section>.'}), 400
    sem, _by=_resolve_batch_sem_from_request(data=data)
    sem_id=int(sem['id']) if sem and sem.get('id') else None
    allowed, reason=_sem_edit_allowed(sem)
    if not allowed: return jsonify({'ok':False,'error':reason}), 403
    effective_from=(data.get('effective_from') or _today_ist_str()).strip()
    payload=data.get('data') if isinstance(data.get('data'),dict) else data.get('timetable')
    if not isinstance(payload,dict):
        src_id=data.get('copy_from_id')
        if src_id:
            conn=_db(); c=conn.cursor()
            try:
                c.execute('SELECT timetable_json FROM timetable_versions WHERE id=?',(int(src_id),))
                r=c.fetchone()
                if r:
                    try: payload=json.loads(r[0] or '{}')
                    except Exception: payload=None
            finally:
                conn.close()
    if not isinstance(payload,dict):
        return jsonify({'ok':False,'error':'Timetable data is required.'}), 400
    try: payload['class']=cls
    except Exception: pass
    res=_save_timetable_version(cls,sem_id,effective_from,payload,
                                reference_path=(data.get('reference_path') or ''),
                                reference_name=(data.get('reference_name') or ''),
                                notes=(data.get('notes') or ''), force_new=bool(data.get('force_new')))
    if res.get('ok'):
        res['staff_sync']=sync_staff_from_timetables(reset_passwords=False)
        audit_log('timetable',cls,'version_save',None,{'version_id':res.get('version_id'),
                  'effective_from':res.get('effective_from'),'action':res.get('action')},
                  details='Admin saved timetable version')
        return jsonify(res)
    return jsonify(res), 400

@app.route('/api/admin/timetable/versions/<int:version_id>/restore', methods=['POST'])
@admin_required
def api_admin_timetable_version_restore(version_id):
    data=request.get_json(silent=True) or {}
    conn=_db(); c=conn.cursor()
    try:
        c.execute("""SELECT class_name,semester_id,version_no,timetable_json,reference_path,reference_name,notes
                     FROM timetable_versions WHERE id=?""",(version_id,))
        row=c.fetchone()
    except Exception:
        row=None
    finally:
        conn.close()
    if not row: return jsonify({'ok':False,'error':'Timetable version not found.'}), 404
    cls,sem_id,version_no,raw,ref_path,ref_name,old_notes=row
    try: payload=json.loads(raw or '{}')
    except Exception: return jsonify({'ok':False,'error':'Stored timetable is invalid.'}), 400
    eff=(data.get('effective_from') or _today_ist_str()).strip()
    sem,_by=_resolve_batch_sem_from_request(data=data)
    if sem and sem.get('id'):
        sem_id=int(sem['id'])
    sem_obj=_semester_by_id(sem_id) if sem_id else sem
    allowed, reason=_sem_edit_allowed(sem_obj)
    if not allowed: return jsonify({'ok':False,'error':reason}), 403
    notes=((data.get('notes') or '').strip() or f'Restored from version {version_no}').strip()
    res=_save_timetable_version(cls,sem_id,eff,payload,reference_path=(ref_path or ''),
                                reference_name=(ref_name or ''),notes=notes)
    if res.get('ok'):
        res['staff_sync']=sync_staff_from_timetables(reset_passwords=False)
        audit_log('timetable',cls,'version_restore',None,{'version_id':version_id,'new_version_id':res.get('version_id')},
                  details='Admin restored timetable version')
        return jsonify(res)
    return jsonify(res), 400

@app.route('/api/admin/timetable/versions/<int:version_id>/hide', methods=['POST'])
@admin_required
def api_admin_timetable_version_hide(version_id):
    return api_admin_timetable_version_delete(version_id)

@app.route('/api/admin/timetable/versions/<int:version_id>/delete', methods=['POST'])
@admin_required
def api_admin_timetable_version_delete(version_id):
    conn=_db(); c=conn.cursor()
    try:
        c.execute("SELECT id,class_name,semester_id,version_no,reference_path,is_active FROM timetable_versions WHERE id=?",(version_id,))
        row=c.fetchone()
        if not row: return jsonify({'ok':False,'error':'Timetable version not found.'}), 404
        vid, cls, sem_id, vno, ref_path = int(row[0]), row[1], row[2], int(row[3] or 0), (row[4] or '')
        c.execute("DELETE FROM student_electives WHERE timetable_version_id=?", (vid,))
        c.execute("DELETE FROM class_elective_offerings WHERE timetable_version_id=?", (vid,))
        c.execute("DELETE FROM timetable_versions WHERE id=?", (vid,))
        next_id=None
        if sem_id is not None:
            c.execute("SELECT id FROM timetable_versions WHERE upper(class_name)=upper(?) AND semester_id=? ORDER BY effective_from DESC, version_no DESC, id DESC LIMIT 1", (cls, sem_id))
            nr=c.fetchone(); next_id=int(nr[0]) if nr else None
            c.execute("UPDATE timetable_versions SET is_active=0 WHERE upper(class_name)=upper(?) AND semester_id=?", (cls, sem_id))
            if next_id is not None:
                c.execute("UPDATE timetable_versions SET is_active=1 WHERE id=?", (next_id,))
        conn.commit(); conn.close()
        if ref_path:
            try:
                conn2=_db(); c2=conn2.cursor(); c2.execute("SELECT 1 FROM timetable_versions WHERE reference_path=? LIMIT 1", (ref_path,)); still=c2.fetchone(); conn2.close()
                if not still:
                    ref_abs=os.path.abspath(os.path.join(APP_DIR, ref_path)); base=os.path.abspath(TIMETABLE_REF_DIR)
                    if (ref_abs==base or ref_abs.startswith(base+os.sep)) and os.path.exists(ref_abs):
                        os.remove(ref_abs)
            except Exception:
                pass
        audit_log('timetable', cls, 'version_delete', str(vid), {'version_no':vno,'semester_id':sem_id}, details=f'Admin deleted timetable version v{vno}')
        return jsonify({'ok':True,'deleted':True,'version_id':vid,'class':cls,'semester_id':sem_id,'next_version_id':next_id})
    except Exception as e:
        try: conn.rollback(); conn.close()
        except Exception: pass
        return jsonify({'ok':False,'error':str(e)}), 400

@app.route('/api/admin/timetable/reference', methods=['POST'])
@admin_required
def api_admin_timetable_reference_upload():
    f=request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok':False,'error':'Choose a PDF or image file.'}), 400
    ext=os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_TIMETABLE_REF_EXTENSIONS:
        return jsonify({'ok':False,'error':'Only PDF, JPG, PNG, or WEBP files are allowed.'}), 400
    cls=_norm(request.form.get('class'))
    if cls not in get_classes():
        return jsonify({'ok':False,'error':'Choose a valid class before uploading a reference.'}), 400
    safe=secure_filename(f.filename) or f'timetable{ext}'
    ts=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    token=uuid.uuid4().hex[:8]
    stored=os.path.join(TIMETABLE_REF_DIR,f'{cls}_{ts}_{token}{ext}')
    f.save(stored)
    rel=os.path.relpath(stored,APP_DIR).replace('\\','/')
    audit_log('timetable',cls,'reference_upload',None,{'file':safe},details='Admin uploaded timetable reference')
    return jsonify({'ok':True,'reference_path':rel,'reference_name':safe,'reference_url':f'/api/admin/timetable/reference/file/{os.path.basename(stored)}'})



def _parse_timetable_import(text, class_hint='', semester_id=None):
    """Parse pasted timetable text/CSV into timetable JSON.
    Supports college-format tables (Day/Hour header, SUBJECT (CODE) cells, metadata lines,
    CODE/SUBJECT/STAFF INCHARGE tables) and simple DAY/PERIOD + staff columns.
    """
    raw=(text or '').replace('\ufeff','')
    lines=[ln.rstrip('\n\r') for ln in raw.splitlines()]
    errors=[]; warnings=[]
    if not any(x.strip() for x in lines):
        return {'ok':False,'errors':['Import text is empty.'],'warnings':[]}
    sample='\n'.join(lines[:15])
    delim='\t' if '\t' in sample else (',' if sample.count(',') >= max(6, sample.count('  ')) else None)
    rows=[]
    if delim==',':
        import csv, io as _io
        rows=list(csv.reader(_io.StringIO(raw)))
    else:
        for ln in lines:
            if '\t' in ln:
                rows.append([x.strip() for x in ln.split('\t')])
            elif '  ' in ln.strip():
                rows.append([x.strip() for x in re.split(r'\s{2,}', ln.strip())])
            else:
                rows.append([ln.strip()])

    def _cells(r):
        return [_norm(x) for x in (r or []) if _norm(x)]
    def _joined(r):
        return ' '.join(_cells(r)).upper()
    def _looks_day_header(j):
        return 'DAY' in j and ('PERIOD' in j or 'HOUR' in j or re.search(r'\b1\s*[\(\-]', j))
    def _looks_staff_table_header(r):
        cells=_cells(r); j=' '.join(c.upper() for c in cells)
        return ('STAFF INCHARGE' in j and ('SUBJECT' in j or 'CODE' in j or 'PERIOD' in j)) or (len(cells)>=3 and cells[0].upper()=='CODE' and 'SUBJECT' in j)
    def _looks_meta_staff_incharge(r):
        cells=_cells(r)
        if not cells: return False
        c0=cells[0].upper().replace(':','').strip()
        return c0 in ('STAFF INCHARGE','STAFF-INCHARGE','STAFF INCHARGES') and len(cells)<=3

    cls=canonical_class_name(class_hint)
    hall=''; advisor_name=''; advisor_code=''
    staff_incharge=[]; staff_dir={}; name_to_code={}
    schedule={d:[] for d in DAY_KEYS}
    slots=[]

    # --- metadata pass ---
    for i,r in enumerate(rows):
        cells=_cells(r)
        if not cells: continue
        joined=_joined(r)
        for c0 in cells:
            m=re.search(r'\b(\d{2}[A-Z]{2,4}\d[A-Z])\b', str(c0).upper())
            if m and is_valid_class_name(canonical_class_name(m.group(1))):
                if not cls: cls=canonical_class_name(m.group(1))
        # parse KEY: VALUE style metadata from cells
        for ci,cell in enumerate(cells):
            cu=cell.upper()
            if cu.startswith('CLASS:') and not re.search(r'\bCLASS ADVISOR\b', cu):
                val=cell.split(':',1)[1].strip() if ':' in cell else ''
                if not val and ci+1 < len(cells): val=cells[ci+1]
                cand=canonical_class_name(val)
                if is_valid_class_name(cand): cls=cand
            if cu.startswith('HALL NO') or cu.startswith('HALL:'):
                val=cell.split(':',1)[1].strip() if ':' in cell else ''
                if not val and ci+1 < len(cells): val=cells[ci+1]
                if val and 'TOTAL' not in val.upper() and 'CLASS ADVISOR' not in val.upper():
                    hall=val
            if 'CLASS ADVISOR' in cu:
                val=cell.split(':',1)[1].strip() if ':' in cell else ''
                if not val and ci+1 < len(cells): val=cells[ci+1]
                if val and not advisor_name: advisor_name=val
        if _looks_meta_staff_incharge(r):
            for c0 in cells[1:]:
                if c0 and c0.upper()!='STAFF INCHARGE': staff_incharge.append(c0)
            for rr in rows[i+1:]:
                c0=_norm(rr[0]) if rr else ''
                rest=[_norm(x) for x in rr[1:] if _norm(x)] if len(rr)>1 else []
                if c0.upper() in DAY_KEYS or _looks_day_header(_joined(rr)) or _looks_staff_table_header(rr): break
                for x in ([c0]+rest):
                    if x and x.upper() not in ('STAFF INCHARGE','DAY/PERIOD','DAY/HOUR'):
                        staff_incharge.append(x)

    # --- parse CODE/SUBJECT/STAFF INCHARGE tables into staff_dir ---
    def _add_staff(name, preferred_code=None):
        nm=_norm(name)
        if not nm: return ''
        nm=re.sub(r'\s*/\s*(Maths|English|Physics|Chemistry|PLA|HOD|Dept\.?|Department)\b.*$','',nm,flags=re.I).strip()
        nm=re.sub(r'^(Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?)\s*','',nm,flags=re.I).strip()
        if not nm: return ''
        key=nm.upper()
        if key in name_to_code: return name_to_code[key]
        code=(preferred_code or '').strip().upper()
        if not code:
            parts=re.findall(r'[A-Za-z]+', nm)
            code=(''.join(p[0] for p in parts[:4]) or f'S{len(staff_dir)+1}').upper()
        code=re.sub(r'[^A-Z0-9]','',code)[:6] or f'S{len(staff_dir)+1}'
        base=code; k=2
        while code in staff_dir and staff_dir[code].upper()!=nm.upper():
            code=f"{base[:5]}{k}"; k+=1
        staff_dir[code]=nm; name_to_code[key]=code
        return code

    def _parse_staff_cell(cell):
        # "Ms.S.Chitra (SC), Mr.P.Prakash (PPR), Ms.S.Mekala (SM)" or "A / B"
        out=[]
        for part in re.split(r'\s*,\s*|\s*/\s*(?=[A-Z][a-z]|Mr|Ms|Mrs|Dr|Prof)', str(cell or '').strip()):
            p=_norm(part)
            if not p: continue
            m=re.match(r'^(.*?)\(([A-Za-z0-9]{1,6})\)\s*$', p)
            if m:
                name=m.group(1).strip(' -'); code=m.group(2).strip().upper()
                code=_add_staff(name, code) or code
                if code: out.append(code)
            else:
                # maybe just name or code
                if re.fullmatch(r'[A-Z]{1,6}', p.upper()) and p.upper() not in DAY_KEYS:
                    out.append(p.upper())
                else:
                    code=_add_staff(p)
                    if code: out.append(code)
        # dedupe preserve order
        seen=set(); res=[]
        for c in out:
            if c and c not in seen: seen.add(c); res.append(c)
        return res

    for r in rows:
        if not _looks_staff_table_header(r): continue
        cells=[_norm(x) for x in r]
        # find staff incharge column index
        staff_idx=None
        for idx,h in enumerate(cells):
            hu=h.upper()
            if 'STAFF INCHARGE' in hu or hu=='STAFF' or 'FACULTY' in hu:
                staff_idx=idx; break
        if staff_idx is None: staff_idx=len(cells)-1
        # consume following rows until blank/day header/next section header
        start=rows.index(r)+1
        for rr in rows[start:]:
            cc=[_norm(x) for x in rr]
            if not any(cc): continue
            j=_joined(rr)
            if _looks_day_header(j) or _looks_staff_table_header(rr): break
            c0=cc[0].upper() if cc else ''
            if c0 in DAY_KEYS: break
            # likely a subject/staff row if first col looks like code or there's a staff cell
            staff_cell=cc[staff_idx] if staff_idx < len(cc) else ''
            code_cell=cc[0] if cc else ''
            subj_cell=cc[1] if len(cc)>1 else ''
            looks_code=bool(re.match(r'^\d{2}[A-Z]{2,}\d*$', code_cell.upper())) or c0 in ('OTHERS','OTHER')
            if staff_cell and (looks_code or subj_cell or re.search(r'\([A-Za-z0-9]{1,6}\)', staff_cell)):
                _parse_staff_cell(staff_cell)

    # advisor code from parsed staff_dir
    if advisor_name:
        adv_clean=re.sub(r'^(Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?)\s*','',advisor_name,flags=re.I).strip()
        advisor_code=name_to_code.get(adv_clean.upper(),'') or name_to_code.get(advisor_name.upper(),'')
        if not advisor_code:
            advisor_code=_add_staff(advisor_name)
    selected_cls=canonical_class_name(class_hint)
    if not cls and selected_cls and is_valid_class_name(selected_cls):
        cls=selected_cls
    if cls and selected_cls and is_valid_class_name(selected_cls) and cls!=selected_cls:
        errors.append(f'Import class "{cls}" does not match selected class "{selected_cls}". Select correct class or fix CLASS line.')
    if not cls:
        errors.append('Class not found. Include CLASS line or choose class in the form.')
    elif not is_valid_class_name(cls):
        errors.append(f'Invalid class "{cls}". Use YYIT<sem><section>.')

    # --- locate day/hour header and parse schedule ---
    day_header_idx=None; day_col=0; period_cols=[]
    for i,r in enumerate(rows):
        j=_joined(r)
        if not _looks_day_header(j):
            continue
        # find day column and period columns
        day_col=0
        for idx,c in enumerate(r):
            cu=_norm(c).upper()
            if 'DAY' in cu: day_col=idx; break
        # map period columns: headers containing 1..7 or (time)
        pcols=[]
        for idx,c in enumerate(r):
            if idx==day_col: continue
            cu=_norm(c).upper()
            m=re.search(r'\b([1-7])\b', cu)
            if m:
                p=int(m.group(1))
                if 1<=p<=7 and all(pc!=idx for pc,_ in pcols):
                    pcols.append((idx,p))
        if not pcols:
            # assume next 7 cols after day col
            pcols=[(day_col+1+k,k+1) for k in range(7) if day_col+1+k < max(len(r),day_col+8)]
        period_cols=sorted(pcols,key=lambda x:x[1])
        day_header_idx=i
        break

    day_rows=[]
    if day_header_idx is not None:
        for r in rows[day_header_idx+1:]:
            c0=_norm(r[day_col] if day_col < len(r) else (r[0] if r else '')).upper()
            c0=re.sub(r'[^A-Z]', '', c0)
            # normalize full names
            day_map={'MONDAY':'MON','TUESDAY':'TUE','WEDNESDAY':'WED','THURSDAY':'THU','FRIDAY':'FRI','SATURDAY':'SAT','SUNDAY':'SUN'}
            c0=day_map.get(c0,c0[:3])
            if c0 in DAY_KEYS:
                day_rows.append((c0,r))
            elif c0 in ('CODE','OTHERS','OTHER','TOTAL','SUBJECT'):
                break
            elif any(_norm(x) for x in r) and not any(ch.isalpha() for ch in c0):
                continue
    else:
        # fallback: any row starting with day
        for r in rows:
            c0=_norm(r[0] if r else '').upper(); c0=re.sub(r'[^A-Z]','',c0)
            day_map={'MONDAY':'MON','TUESDAY':'TUE','WEDNESDAY':'WED','THURSDAY':'THU','FRIDAY':'FRI','SATURDAY':'SAT','SUNDAY':'SUN'}
            c0=day_map.get(c0,c0[:3])
            if c0 in DAY_KEYS: day_rows.append((c0,r))

    if not day_rows:
        errors.append('No DAY/PERIOD rows found. Use a header like Day/Hour with Monday..Saturday rows.')

    for day,r in day_rows:
        prev=None
        for col_idx,p in period_cols:
            txt=_norm(r[col_idx]) if col_idx < len(r) else ''
            if not txt or txt.upper() in {'FREE','-','NIL','NA','N/A'}:
                prev=None; continue
            if txt in {'"', '""', "'"} or txt.upper()=='SAME':
                if prev:
                    cpy=dict(prev); cpy['period']=p; cpy['display']=cpy.get('subject','')+ ((' ('+', '.join(cpy.get('staff',[]))+')') if cpy.get('staff') else '')
                    schedule[day].append(cpy)
                continue
            # parse SUBJECT (STAFF,STAFF) or SUBJECT - STAFF (HALL)
            hall_m=re.search(r'\(([^()]*)\)\s*$', txt)
            paren=hall_m.group(1).strip() if hall_m else ''
            core=txt[:hall_m.start()].strip() if hall_m else txt
            subj=core; staff_names=[]
            hall_cell=''
            if ' - ' in core:
                left,right=core.rsplit(' - ',1)
                subj=left.strip(); staff_names=[x.strip() for x in re.split(r',|/|&', right) if x.strip()]
            # decide whether paren is staff codes/names or hall
            paren_is_staff=False
            if paren:
                if re.search(r'\b[A-Z]{1,6}\b', paren.upper()) and len(paren)<=60:
                    paren_is_staff=True
                if re.search(r',|/|&', paren): paren_is_staff=True
                if 'LAB' in paren.upper() and not re.search(r'\([A-Z]{1,6}\)', '('+paren+')'): pass
            if paren_is_staff:
                codes=_parse_staff_cell(paren)
                if codes:
                    staff_names=codes
                else:
                    hall_cell=paren
            elif paren:
                hall_cell=paren
            # resolve staff names/codes
            codes=[]
            for sn in staff_names:
                if re.fullmatch(r'[A-Z0-9]{1,6}', sn.upper()):
                    c=sn.upper()
                    if c not in staff_dir: staff_dir[c]=c
                    codes.append(c)
                else:
                    c=_add_staff(sn)
                    if c: codes.append(c)
            is_lab='LAB' in subj.upper() or 'LAB' in hall_cell.upper()
            disp=subj + (f" ({', '.join(codes)})" if codes else '')
            cell_obj={'period':p,'subject':subj,'staff':codes,'display':disp}
            if is_lab: cell_obj['is_lab']=True
            schedule[day].append(cell_obj); prev=cell_obj

    # normalize/merge slots by period
    if period_cols:
        slot_map={}
        for col_idx,p in period_cols:
            hdr=_norm(rows[day_header_idx][col_idx]) if day_header_idx is not None and col_idx < len(rows[day_header_idx]) else f'Period {p}'
            tm=re.findall(r'(\d{1,2}[\.:]\d{2})', hdr)
            start=tm[0].replace('.',':') if len(tm)>0 else ['09:00','09:45','10:50','11:40','13:20','14:15','15:20'][p-1]
            end=tm[1].replace('.',':') if len(tm)>1 else ['09:45','10:35','11:40','12:30','14:15','15:10','16:15'][p-1]
            slot_map[p]={'period':p,'label':f'Hour {p}','start':start,'end':end}
        slots=[slot_map[p] for p in sorted(slot_map)]
    if not slots:
        slots=[{'period':i,'label':f'Hour {i}','start':['09:00','09:45','10:50','11:40','13:20','14:15','15:20'][i-1],'end':['09:45','10:35','11:40','12:30','14:15','15:10','16:15'][i-1]} for i in range(1,8)]

    payload={
        'class':cls or '', 'hall':hall,
        'class_advisor':{'name':advisor_name,'initials':advisor_code,'is_hod':False},
        'slots':slots,
        'breaks':[{'after_period':2,'start':'10:35','end':'10:50'},{'after_period':6,'start':'15:10','end':'15:20'}],
        'lunch':{'start':'12:30','end':'13:20'},
        'schedule':schedule,
        'staff_directory':staff_dir
    }
    if cls:
        verrors,vwarn=_validate_timetable(payload, cls)
        errors.extend(verrors); warnings.extend(vwarn)
    return {'ok':len(errors)==0,'errors':errors,'warnings':warnings,'data':payload,'class':cls,
            'semester_id':semester_id,'staff_count':len(staff_dir),'advisor':payload['class_advisor']}

@app.route('/api/admin/timetable/reference/file/<path:filename>')
@admin_required
def api_admin_timetable_reference_file(filename):
    safe=os.path.basename(filename)
    path=os.path.join(TIMETABLE_REF_DIR,safe)
    if not os.path.exists(path): return jsonify({'ok':False,'error':'Reference not found.'}), 404
    return send_file(path)

@app.route('/admin/timetable/reference/<int:version_id>')
@admin_required
def admin_timetable_reference(version_id):
    conn=_db(); c=conn.cursor()
    try:
        c.execute('SELECT reference_path FROM timetable_versions WHERE id=?',(version_id,))
        row=c.fetchone()
    except Exception:
        row=None
    finally:
        conn.close()
    if not row or not row[0]: return jsonify({'ok':False,'error':'Reference not found.'}), 404
    path=os.path.abspath(os.path.join(APP_DIR,row[0]))
    base=os.path.abspath(TIMETABLE_REF_DIR)
    if not (path==base or path.startswith(base+os.sep)) or not os.path.exists(path):
        return jsonify({'ok':False,'error':'Reference not found.'}), 404
    return send_file(path)

# --- Admin: elective option mapping (student -> option per timetable slot) ---
def _elective_slots_from_tt(tt):
    slots=[]
    for day, cells in (tt.get('schedule') or {}).items():
        for cell in cells or []:
            options=cell.get('options') or []
            if options:
                slots.append({'slot_key':f'{str(day).upper()}-P{int(cell.get("period") or 0)}','day':str(day).upper(),
                              'period':int(cell.get('period') or 0),'subject':cell.get('subject') or '',
                              'display':cell.get('display') or cell.get('subject') or '', 'options':options})
    return slots

def _elective_slots(class_name, on_date=None):
    return _elective_slots_from_tt(load_timetable(class_name,on_date) or {})

def _elective_version(class_name, version_id=None, on_date=None):
    if version_id is not None:
        try:
            vid=int(version_id)
        except Exception:
            return None
        for v in _timetable_version_rows(class_name):
            if int(v.get('id') or 0)==vid: return v
        return None
    return _timetable_version_row(class_name,on_date)

def _elective_slots_for_version(class_name, version=None):
    if version:
        try:
            tt=json.loads(version.get('timetable_json') or '{}')
            return _elective_slots_from_tt(tt)
        except Exception:
            pass
    return _elective_slots(class_name)

def _elective_maps(class_name, version_id=None):
    conn=_db(); c=conn.cursor(); maps={}
    try:
        if version_id is None:
            c.execute("""SELECT student_id,slot_key,option_label FROM student_electives
                         WHERE upper(class_name)=upper(?) AND timetable_version_id IS NULL""",(class_name,))
        else:
            c.execute("""SELECT student_id,slot_key,option_label,timetable_version_id FROM student_electives
                         WHERE upper(class_name)=upper(?)
                           AND (timetable_version_id=? OR timetable_version_id IS NULL)
                         ORDER BY CASE WHEN timetable_version_id=? THEN 0 ELSE 1 END""",(class_name,int(version_id),int(version_id)))
        for row in c.fetchall():
            sid,slot,label=row[0],row[1],row[2]
            if sid is None or not slot: continue
            maps.setdefault(int(sid),{}).setdefault(str(slot).upper(),label)
    except Exception:
        maps={}
    conn.close(); return maps

def _student_elective_map(student_id, class_name, version_id=None):
    conn=_db(); c=conn.cursor()
    try:
        if version_id is None:
            c.execute("""SELECT slot_key,option_label FROM student_electives
                         WHERE student_id=? AND upper(class_name)=upper(?) AND timetable_version_id IS NULL""",
                      (student_id,class_name))
        else:
            c.execute("""SELECT slot_key,option_label,timetable_version_id FROM student_electives
                         WHERE student_id=? AND upper(class_name)=upper(?)
                           AND (timetable_version_id=? OR timetable_version_id IS NULL)
                         ORDER BY CASE WHEN timetable_version_id=? THEN 0 ELSE 1 END""",
                      (student_id,class_name,int(version_id),int(version_id)))
        out={}
        for row in c.fetchall(): out.setdefault(str(row[0]).upper(),row[1])
    except Exception:
        out={}
    conn.close(); return out

def _apply_elective_map_to_schedule(schedule, emap):
    out={}
    for day, cells in (schedule or {}).items():
        dk=str(day).upper(); new_cells=[]
        for cell in cells or []:
            cel=dict(cell); p=int(cel.get('period') or 0)
            label=emap.get(f'{dk}-P{p}')
            if label and cel.get('options'):
                cel['display']=label; cel['subject']=label; cel['selected_elective']=label
            new_cells.append(cel)
        out[day]=new_cells
    return out



def _elective_offering_map(class_name, version_id=None):
    conn=_db(); c=conn.cursor(); out={}
    try:
        if version_id is None: return out
        c.execute("SELECT slot_key,option_label,staff_codes FROM elective_offerings WHERE upper(class_name)=upper(?) AND timetable_version_id=?",(class_name,int(version_id)))
        for slot,label,codes in c.fetchall():
            sk=str(slot or '').upper(); lab=str(label or '').strip()
            if not sk or not lab: continue
            vals=[_norm(x).upper() for x in re.split(r'[;,\s]+', codes or '') if _norm(x)]
            out.setdefault(sk,{})[lab]=sorted(set(vals))
    except Exception:
        out={}
    conn.close(); return out

def _period_slot_key(period, date_str=None):
    try: p=int(period)
    except Exception: p=0
    day=_day_key(date_str).upper() if date_str else ''
    return f"{day}-P{p}" if day else f"P{p}"

def _elective_allowed_staff_codes(offering_map, elective_label, cell=None):
    codes=set((offering_map or {}).get(elective_label or '', []) or [])
    if codes: return {str(c).upper() for c in codes}
    for opt in ((cell or {}).get('options') or []):
        if (opt or '').strip()==(elective_label or '').strip():
            m=re.search(r'\b([A-Z]{2,6})\b', (opt or '').upper())
            if m: codes.add(m.group(1))
    return {str(c).upper() for c in codes}

def _can_mark_elective(class_name, date_str, period, cell=None):
    code=(session.get('staff_code') or session.get('username') or '').upper()
    role=session.get('role')
    if role=='admin' or session.get('is_hod'): return True
    if session.get('is_advisor') and session.get('advisor_class')==class_name: return True
    cell = cell or timetable_cell(class_name,date_str,period) or {}
    if not cell.get('options'): return False
    vid=_timetable_version_id(class_name,date_str)
    om=_elective_offering_map(class_name,vid)
    slot=f"{_day_key(date_str).upper()}-P{int(period or 0)}"
    opts=om.get(slot,{})
    if opts:
        for codes in opts.values():
            if code and code in codes: return True
        return False
    for opt in (cell.get('options') or []):
        if code and re.search(r'\b'+re.escape(code)+r'\b', (opt or '').upper()): return True
    return False

def _elective_lookup():
    conn=_db(); c=conn.cursor(); out={}
    try:
        c.execute('SELECT student_id,class_name,timetable_version_id,slot_key,option_label FROM student_electives')
        for sid,cn,vid,slot,label in c.fetchall():
            if sid is None or not slot: continue
            out.setdefault((int(sid),(cn or '').upper(),int(vid) if vid is not None else None),{})[str(slot).upper()]=label
    except Exception:
        out={}
    conn.close(); return out

def _effective_subject(emaps, student_id, class_name, date_str, period, fallback='', version_id=None):
    try:
        sid=int(student_id); p=int(period)
    except Exception:
        return fallback
    cn=(class_name or '').upper(); slot=f'{_day_key(date_str).upper()}-P{p}'
    vid=version_id if version_id is not None else _timetable_version_id(class_name,date_str)
    keys=[]
    if vid is not None: keys.append((sid,cn,int(vid)))
    keys.append((sid,cn,None))
    for key in keys:
        label=(emaps.get(key) or {}).get(slot)
        if label: return label
    return fallback

def _elective_map_details(class_name, version_id=None):
    conn=_db(); c=conn.cursor(); out={}
    try:
        if version_id is None:
            c.execute("""SELECT student_id,slot_key,option_label,timetable_version_id FROM student_electives
                         WHERE upper(class_name)=upper(?) AND timetable_version_id IS NULL""",(class_name,))
        else:
            c.execute("""SELECT student_id,slot_key,option_label,timetable_version_id FROM student_electives
                         WHERE upper(class_name)=upper(?)
                           AND (timetable_version_id=? OR timetable_version_id IS NULL)
                         ORDER BY CASE WHEN timetable_version_id=? THEN 0 ELSE 1 END""",
                      (class_name,int(version_id),int(version_id)))
        for sid,slot,label,vid in c.fetchall():
            if sid is None or not slot: continue
            row=out.setdefault(int(sid),{}).setdefault(str(slot).upper(),{'label':label,'source':'legacy'})
            if vid is not None and row.get('source')=='legacy':
                row['label']=label; row['source']='version'
    except Exception:
        out={}
    conn.close(); return out

def _resolve_elective_version(class_name, version_id=None, semester_id=None):
    if version_id not in (None,''):
        try: vid=int(version_id)
        except Exception: return None
        version=_elective_version(class_name,vid)
        if not version: return None
        if semester_id not in (None,''):
            try: sid=int(semester_id)
            except Exception: return None
            if int(version.get('semester_id') or 0)!=sid: return None
        return version
    version=_timetable_version_row(class_name,_today_ist_str())
    if semester_id not in (None,''):
        try: sid=int(semester_id)
        except Exception: return None
        if version and int(version.get('semester_id') or 0)==sid: return version
        rows=_timetable_version_rows(class_name,sid)
        return rows[0] if rows else None
    if version: return version
    rows=_timetable_version_rows(class_name,None)
    return rows[0] if rows else None

def _is_admin_role():
    return (session.get('role') or '').lower() in ('admin','hod') or bool(session.get('is_hod'))

def _sem_edit_allowed(sem_row):
    if _is_admin_role(): return True, None
    if not sem_row: return False, 'Semester context required.'
    by=sem_row.get('batch_start_year'); sn=sem_row.get('sem_no')
    if by is None or sn is None: return False, 'Batch semester context required.'
    cur=current_sem_for_batch(by) or sn
    if int(sn) <= int(cur):
        return False, 'Staff/students can view but cannot edit timetable/elective data for current or previous semesters.'
    return True, None

@app.route('/admin/electives')
@admin_required
def admin_electives_page():
    batches=[{'batch_start_year':by,'label':batch_label(by)} for by in BATCH_START_YEARS]
    return render_template('admin_electives.html', classes=get_classes(), batches=batches, today=_today_ist_str(), semesters=[])

@app.route('/api/admin/electives')
@admin_required
def api_admin_electives():
    cls=(request.args.get('class') or '').strip()
    if not cls or cls not in get_classes(): return jsonify({'ok':False,'error':'Valid class required.'}), 400
    sem, by=_resolve_batch_sem_from_request()
    semester_id=int(sem['id']) if sem and sem.get('id') else None
    sem_no=int(sem.get('sem_no') or 0) if sem else None
    if sem_no and sem_no < 5:
        return jsonify({'ok':True,'class':cls,'batch_start_year':by,'semester_id':semester_id,'sem_no':sem_no,'electives_enabled':False,
                        'message':'Electives apply only for Sem 5-8.','version':None,'versions':[],'slots':[],'offerings':{},'students':[],
                        'stats':{'students':0,'slots':0,'mapped':0,'missing':0,'unassigned_students':0,'conflicts':0}})
    version=_resolve_elective_version(cls,request.args.get('version_id'),semester_id)
    vid=int(version['id']) if version else None
    if semester_id is None and version and version.get('semester_id') is not None:
        semester_id=int(version['semester_id'])
        sem=_semester_by_id(semester_id) or sem
        sem_no=int((sem or {}).get('sem_no') or sem_no or 0) or sem_no
    versions=_timetable_version_rows(cls,semester_id) if semester_id is not None else _timetable_version_rows(cls)
    if version and all(int(v.get('id') or 0)!=vid for v in versions):
        versions=[version]+versions
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,reg_no,roll,class,student_role FROM students WHERE upper(class)=upper(?) ORDER BY name COLLATE NOCASE',(cls,))
    students=[{'id':r[0],'name':r[1],'reg_no':r[2],'roll':r[3],'class':r[4],'student_role':r[5] or ''} for r in c.fetchall()]
    conn.close()
    slots=_elective_slots_for_version(cls,version)
    offerings=_elective_offering_map(cls,vid) if vid is not None else {}
    details=_elective_map_details(cls,vid) if vid is not None else _elective_map_details(cls,None)
    option_map={s['slot_key']:set(s.get('options') or []) for s in slots}
    mapped=missing=unassigned=conflicts=0
    for s in students:
        dm=details.get(int(s['id']),{})
        labels={k:v.get('label') for k,v in dm.items()}
        sources={k:v.get('source') for k,v in dm.items()}
        s['electives']=labels; s['electives_source']=sources
        filled=0
        for sl in slots:
            label=labels.get(sl['slot_key'])
            if label:
                filled+=1
                if label not in option_map.get(sl['slot_key'],set()): conflicts+=1
        mapped+=filled; missing+=max(0,len(slots)-filled)
        if slots and not filled: unassigned+=1
    can_edit, edit_reason=_sem_edit_allowed(sem)
    return jsonify({'ok':True,'class':cls,'batch_start_year':by,'semester_id':semester_id,'sem_no':sem_no,'electives_enabled':True,'can_edit':can_edit,'edit_reason':edit_reason,
                    'version':_timetable_version_json(version) if version else None,
                    'versions':[_timetable_version_json(v) for v in versions],'slots':slots,'offerings':offerings,'students':students,
                    'stats':{'students':len(students),'slots':len(slots),'mapped':mapped,'missing':missing,
                             'unassigned_students':unassigned,'conflicts':conflicts}})

@app.route('/api/admin/electives', methods=['POST'])
@admin_required
def api_admin_electives_save():
    data=request.get_json(silent=True) or {}
    cls=_norm(data.get('class_name') or data.get('class'))
    if not cls or cls not in get_classes(): return jsonify({'ok':False,'error':'Valid class required.'}), 400
    sem, _by=_resolve_batch_sem_from_request(data=data)
    sem_no=int((sem or {}).get('sem_no') or 0)
    if sem and sem_no and sem_no < 5:
        return jsonify({'ok':False,'error':'Electives are available only for Sem 5-8.'}), 400
    allowed, reason=_sem_edit_allowed(sem)
    if not allowed: return jsonify({'ok':False,'error':reason}), 403
    version=_resolve_elective_version(cls,data.get('version_id'),int(sem['id']) if sem and sem.get('id') else data.get('semester_id'))
    if not version: return jsonify({'ok':False,'error':'Create/select a timetable version before mapping electives.'}), 400
    vid=int(version['id'])
    slots={s['slot_key']:s for s in _elective_slots_for_version(cls,version)}
    items=data.get('mappings') or []
    if not items and data.get('student_id') is not None:
        items=[{'student_id':data.get('student_id'),'slot_key':data.get('slot_key'),'option_label':data.get('option_label')}]
    offerings=data.get('offerings') or []
    conn=_db(); c=conn.cursor(); saved=cleared=off_saved=0; errors=[]
    def _slot_keys(slot_raw):
        k=_norm(slot_raw).upper()
        if k in slots: return [k]
        if re.fullmatch(r"P?\d+", k):
            p=k[1:] if k.startswith('P') else k
            return [sk for sk in slots.keys() if sk.endswith(f"-P{p}") or sk==f"P{p}"]
        return [k]
    for off in offerings:
        raw_slot=off.get('slot_key'); label=_norm(off.get('option_label'))
        skeys=_slot_keys(raw_slot)
        if not skeys or any(k not in slots for k in skeys): errors.append(f'Unknown elective slot {raw_slot}.'); continue
        for slot in skeys:
            if label not in slots[slot]['options']: errors.append(f'Invalid offering option for {slot}: {label}'); continue
            codes=off.get('staff_codes') or off.get('staff') or []
            if isinstance(codes,str): codes=re.split(r'[;,\s]+', codes)
            vals=sorted(set([_norm(x).upper() for x in codes if _norm(x)]))
            c.execute("""INSERT INTO elective_offerings(class_name,timetable_version_id,slot_key,option_label,staff_codes,updated_at,updated_by)
                         VALUES (?,?,?,?,?,?,?) ON CONFLICT(class_name,timetable_version_id,slot_key,option_label)
                         DO UPDATE SET staff_codes=excluded.staff_codes,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                      (cls,vid,slot,label,','.join(vals),_now_iso(),session.get('username')))
            off_saved+=1
    for item in items:
        try: sid=int(item.get('student_id'))
        except Exception: errors.append('Invalid student id.'); continue
        c.execute('SELECT id,class FROM students WHERE id=?',(sid,)); st=c.fetchone()
        if not st or (st[1] or '').upper()!=cls.upper(): errors.append(f'Student {sid} is not in {cls}.'); continue
        raw_slot=item.get('slot_key'); label=_norm(item.get('option_label'))
        skeys=_slot_keys(raw_slot)
        if not skeys or any(k not in slots for k in skeys): errors.append(f'Unknown elective slot {raw_slot}.'); continue
        for slot in skeys:
            if not label:
                c.execute('DELETE FROM student_electives WHERE student_id=? AND class_name=? AND timetable_version_id=? AND slot_key=?',(sid,cls,vid,slot)); cleared+=1; continue
            if label not in slots[slot]['options']: errors.append(f'Invalid option for {slot}: {label}'); continue
            c.execute("""INSERT INTO student_electives(student_id,class_name,timetable_version_id,slot_key,option_label,updated_at,updated_by)
                         VALUES (?,?,?,?,?,?,?) ON CONFLICT(student_id,class_name,timetable_version_id,slot_key)
                         DO UPDATE SET option_label=excluded.option_label,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                      (sid,cls,vid,slot,label,_now_iso(),session.get('username')))
            saved+=1
    conn.commit(); conn.close()
    if saved or cleared or off_saved: audit_log('electives',cls,'save',str(vid),{'saved':saved,'cleared':cleared,'off_saved':off_saved,'errors':errors,'version_no':version.get('version_no')},details='Admin saved version-scoped elective mappings/offerings')
    return jsonify({'ok':not errors,'saved':saved,'cleared':cleared,'off_saved':off_saved,'version':_timetable_version_json(version),'errors':errors}), (200 if not errors else 400)

@app.route('/api/admin/electives/carry-forward', methods=['POST'])
@admin_required
def api_admin_electives_carry_forward():
    data=request.get_json(silent=True) or {}
    cls=_norm(data.get('class_name') or data.get('class'))
    if not cls or cls not in get_classes(): return jsonify({'ok':False,'error':'Valid class required.'}), 400
    sem, _by=_resolve_batch_sem_from_request(data=data)
    sem_no=int((sem or {}).get('sem_no') or 0)
    if sem and sem_no and sem_no < 5:
        return jsonify({'ok':False,'error':'Electives are available only for Sem 5-8.'}), 400
    allowed, reason=_sem_edit_allowed(sem)
    if not allowed: return jsonify({'ok':False,'error':reason}), 403
    target=_resolve_elective_version(cls,data.get('version_id'),int(sem['id']) if sem and sem.get('id') else data.get('semester_id'))
    if not target: return jsonify({'ok':False,'error':'Target timetable version required.'}), 400
    tid=int(target['id'])
    target_slots={s['slot_key']:s for s in _elective_slots_for_version(cls,target)}
    source=None; source_id=None
    if data.get('from_version_id') not in (None,''):
        source=_resolve_elective_version(cls,data.get('from_version_id'))
        if source: source_id=int(source['id'])
    else:
        rows=_timetable_version_rows(cls)
        for v in rows:
            if int(v.get('id') or 0)==tid: continue
            if (v.get('effective_from') or '') < (target.get('effective_from') or ''):
                source=v; source_id=int(v['id']); break
    source_maps=_elective_maps(cls,source_id) if source_id is not None else _elective_maps(cls,None)
    conn=_db(); c=conn.cursor(); copied=skipped=invalid=0
    for sid,sm in source_maps.items():
        c.execute('SELECT class FROM students WHERE id=?',(sid,)); st=c.fetchone()
        if not st or (st[0] or '').upper()!=cls.upper(): continue
        for slot,label in sm.items():
            if slot not in target_slots: skipped+=1; continue
            if label not in (target_slots[slot].get('options') or []): invalid+=1; continue
            c.execute('SELECT 1 FROM student_electives WHERE student_id=? AND class_name=? AND timetable_version_id=? AND slot_key=?',(sid,cls,tid,slot))
            if c.fetchone(): skipped+=1; continue
            c.execute("""INSERT INTO student_electives(student_id,class_name,timetable_version_id,slot_key,option_label,updated_at,updated_by)
                         VALUES (?,?,?,?,?,?,?)""",(sid,cls,tid,slot,label,_now_iso(),session.get('username')))
            copied+=1
    conn.commit(); conn.close()
    audit_log('electives',cls,'carry_forward',str(tid),{'from_version_id':source_id,'copied':copied,'skipped':skipped,'invalid':invalid},details='Admin carried elective mappings forward')
    return jsonify({'ok':True,'copied':copied,'skipped':skipped,'invalid':invalid,'from_version_id':source_id,
                    'version':_timetable_version_json(target)})

@app.route('/api/admin/electives/export')
@admin_required
def api_admin_electives_export():
    cls=(request.args.get('class') or '').strip()
    if not cls or cls not in get_classes(): abort(400)
    sem_no=request.args.get('sem_no'); by=request.args.get('batch_start_year')
    sem_id=request.args.get('semester_id')
    if sem_id in (None,'') and sem_no not in (None,'') and by not in (None,''):
        try: sem=_batch_semester_row(int(by),int(sem_no)); sem_id=sem.get('id') if sem else None
        except Exception: pass
    version=_resolve_elective_version(cls,request.args.get('version_id'),sem_id)
    vid=int(version['id']) if version else None
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,reg_no,roll,class FROM students WHERE upper(class)=upper(?) ORDER BY name COLLATE NOCASE',(cls,))
    students=c.fetchall(); conn.close(); maps=_elective_maps(cls,vid) if vid is not None else _elective_maps(cls,None)
    slots=_elective_slots_for_version(cls,version)
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['reg_no','roll','name','class']+[s['slot_key'] for s in slots])
    for sid,name,reg,roll,stcls in students:
        m=maps.get(sid,{})
        w.writerow([reg or '',roll or '',name or '',stcls or '']+[m.get(s['slot_key'],'') for s in slots])
    suffix=f"_v{version.get('version_no')}" if version else '_legacy'
    return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name=f'electives_{cls}{suffix}.csv')

@app.route('/api/admin/electives/import', methods=['POST'])
@admin_required
def api_admin_electives_import():
    cls=_norm(request.form.get('class_name') or request.form.get('class'))
    if not cls or cls not in get_classes(): return jsonify({'ok':False,'error':'Valid class required.'}), 400
    sem_no=request.form.get('sem_no'); by=request.form.get('batch_start_year')
    sem_id=request.form.get('semester_id')
    if sem_id in (None,'') and sem_no not in (None,'') and by not in (None,''):
        try: sem=_batch_semester_row(int(by),int(sem_no)); sem_id=sem.get('id') if sem else None
        except Exception: pass
    version=_resolve_elective_version(cls,request.form.get('version_id'),sem_id)
    if not version: return jsonify({'ok':False,'error':'Create/select a timetable version before importing electives.'}), 400
    vid=int(version['id'])
    f=request.files.get('file')
    if not f: return jsonify({'ok':False,'error':'CSV file required.'}), 400
    try: reader=csv.DictReader(io.StringIO(f.stream.read().decode('utf-8-sig')))
    except Exception as e: return jsonify({'ok':False,'error':f'Could not parse CSV: {e}'}), 400
    slots={s['slot_key']:s for s in _elective_slots_for_version(cls,version)}; saved=0; errors=[]
    conn=_db(); c=conn.cursor()
    for i,row in enumerate(reader,start=2):
        reg=(row.get('reg_no') or row.get('Reg No') or '').strip()
        c.execute('SELECT id FROM students WHERE upper(reg_no)=upper(?) AND upper(class)=upper(?)',(reg,cls)); st=c.fetchone()
        if not st: errors.append(f'Row {i}: student {reg} not found in {cls}.'); continue
        for slot,label in row.items():
            if not slot or slot.upper() not in slots: continue
            label=(label or '').strip(); key=slot.upper()
            if not label: continue
            if label not in slots[key]['options']: errors.append(f'Row {i}: invalid option for {key}.'); continue
            c.execute("""INSERT INTO student_electives(student_id,class_name,timetable_version_id,slot_key,option_label,updated_at,updated_by)
                         VALUES (?,?,?,?,?,?,?) ON CONFLICT(student_id,class_name,timetable_version_id,slot_key)
                         DO UPDATE SET option_label=excluded.option_label,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                      (st[0],cls,vid,key,label,_now_iso(),session.get('username')))
            saved+=1
    conn.commit(); conn.close()
    audit_log('electives',cls,'import',str(vid),{'saved':saved,'errors':len(errors),'version_no':version.get('version_no')},details='Admin imported version-scoped elective CSV')
    return jsonify({'ok':True,'saved':saved,'version':_timetable_version_json(version),'errors':errors})

@app.route('/student_photo/<int:sid>')
@admin_required
def student_photo(sid):
    p=_student_profile_photo_path(sid)
    if not p: abort(404)
    return send_file(p)

@app.route('/students/<int:sid>', methods=['DELETE'])
@admin_required
def delete_student(sid):
    conn=_db(); c=conn.cursor()
    c.execute('SELECT name,reg_no,class FROM students WHERE id=?',(sid,)); old=c.fetchone()
    c.execute('DELETE FROM students WHERE id=?',(sid,))
    c.execute('DELETE FROM attendance WHERE student_id=?',(sid,))
    c.execute('DELETE FROM notifications WHERE student_id=?',(sid,))
    for table in ('student_electives','student_alerts','notification_outbox','push_subscriptions','attendance_entries'):
        try: c.execute(f'DELETE FROM {table} WHERE student_id=?',(sid,))
        except Exception: pass
    c.execute('DELETE FROM users WHERE student_id=? AND role="student"',(sid,))
    conn.commit(); conn.close()
    folder=os.path.join(DATASET_DIR,str(sid))
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    for ext in ('.jpg','.jpeg','.png','.webp'):
        p=os.path.join(PROFILE_PHOTOS_DIR,f'{int(sid)}{ext}')
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
    audit_log('student', sid, 'delete', {'name':old[0],'reg_no':old[1],'class':old[2]} if old else None, None, details='Admin deleted student and linked records')
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
# LOCAL PHASE 6: ADMIN WEBCAM / VIDEO FACE ATTENDANCE
# ==============================================================================
_face_model_lock = threading.Lock()
_face_model_cache = {'mtime': None, 'clf': None}
_face_job_lock = threading.Lock()


def _face_model():
    path = MODEL_PATH if os.path.isabs(MODEL_PATH) else os.path.join(APP_DIR, MODEL_PATH)
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None
    with _face_model_lock:
        if _face_model_cache.get('mtime') == mtime and _face_model_cache.get('clf') is not None:
            return _face_model_cache.get('clf')
        try:
            with open(path, 'rb') as f:
                clf = pickle.load(f)
        except Exception:
            clf = None
        _face_model_cache['mtime'] = mtime
        _face_model_cache['clf'] = clf
        return clf


def _face_rel(path):
    if not path:
        return None
    return os.path.relpath(os.path.abspath(path), APP_DIR).replace('\\', '/')


def _face_abs(rel_path):
    if not rel_path:
        return None
    path = rel_path if os.path.isabs(rel_path) else os.path.join(APP_DIR, rel_path)
    return os.path.abspath(path)


def _face_remove_path(rel_path, roots=(FACE_VIDEO_DIR, FACE_CROP_DIR)):
    path = _face_abs(rel_path)
    if not path:
        return False
    allowed = [os.path.abspath(root) + os.sep for root in roots]
    if not any(path.startswith(root) for root in allowed):
        return False
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _face_job(job_id):
    conn = _db()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM face_attendance_jobs WHERE id=?', (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _face_job_faces(job_id):
    conn = _db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT f.*,s.student_role AS student_role FROM face_attendance_faces f
           LEFT JOIN students s ON s.id=f.student_id WHERE f.job_id=?
           ORDER BY CASE WHEN f.kind='student' THEN 0 ELSE 1 END, f.sample_count DESC, f.id""",
        (job_id,)
    ).fetchall()
    conn.close()
    out = []
    for row in rows:
        item = dict(row)
        item['crop_url'] = f"/api/admin/face/crops/{item['id']}" if item.get('crop_path') else None
        out.append(item)
    return out


def _face_job_payload(job_id):
    job = _face_job(job_id)
    if not job:
        return None
    job['faces'] = _face_job_faces(job_id)
    return job


def _face_roster_map(class_name):
    conn = _db(); c = conn.cursor()
    c.execute("SELECT id,name,reg_no,roll,student_role FROM students WHERE upper(class)=upper(?) AND (active IS NULL OR active=1) ORDER BY roll,reg_no,name", (class_name,))
    rows = c.fetchall(); conn.close()
    return {int(r[0]): {'id': int(r[0]), 'name': r[1] or '', 'reg_no': r[2] or '', 'roll': r[3] or '', 'student_role': r[4] or ''} for r in rows}


def _face_validate_context(class_name, date_str, period):
    cls = _norm(class_name)
    date_str = _norm(date_str)
    try:
        period = int(period)
    except Exception:
        return None, 'Invalid period.'
    if not cls or cls not in get_classes():
        return None, 'Invalid class.'
    try:
        datetime.date.fromisoformat(date_str)
    except Exception:
        return None, 'Invalid attendance date.'
    if period < 1 or period > 12:
        return None, 'Invalid period.'
    ok, reason = can_mark_period(cls, date_str, period)
    if not ok:
        return None, reason
    allowed, allow_reason = attendance_edit_allowed(date_str)
    if not allowed:
        return None, allow_reason
    blocked, block_reason = _calendar_block_submit(cls, date_str)
    if blocked:
        return None, block_reason
    cell = timetable_cell(cls, date_str, period)
    if not cell:
        return None, 'No scheduled class for this class/date/period.'
    return {'class_name': cls, 'date': date_str, 'period': period, 'cell': cell, 'allow_reason': allow_reason}, None


def _face_refresh_counts(c, job_id):
    c.execute("SELECT COUNT(*) FROM face_attendance_faces WHERE job_id=? AND kind='student'", (job_id,))
    matched = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM face_attendance_faces WHERE job_id=? AND kind='unknown'", (job_id,))
    unknown = int(c.fetchone()[0] or 0)
    c.execute('UPDATE face_attendance_jobs SET matched_students=?, unknown_faces=? WHERE id=?', (matched, unknown, job_id))
    return matched, unknown


def _face_encode_crop(crop):
    if crop is None or getattr(crop, 'size', 0) == 0:
        return None
    try:
        ok, buf = cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _face_save_crop(job_id, face_key, jpeg_bytes):
    if not jpeg_bytes:
        return None
    try:
        folder = os.path.join(FACE_CROP_DIR, f'job_{int(job_id)}')
        os.makedirs(folder, exist_ok=True)
        safe_key = re.sub(r'[^A-Za-z0-9_-]+', '_', str(face_key or 'face'))[:48]
        name = f"{safe_key}_{secrets.token_hex(4)}.jpg"
        path = os.path.join(folder, name)
        with open(path, 'wb') as f:
            f.write(jpeg_bytes)
        return _face_rel(path)
    except Exception:
        return None


def _face_decode_image(data):
    try:
        if not data:
            return None
        arr = np.frombuffer(data, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _recognize_faces_image(image_bytes, max_faces=8):
    img = _face_decode_image(image_bytes)
    if img is None:
        return []
    boxes = detect_faces_bboxes(img, max_faces=max_faces)
    clf = _face_model()
    out = []
    for box in boxes:
        emb = crop_face_and_embed(img, box)
        predicted_id = None
        confidence = 0.0
        if clf is not None and emb is not None:
            try:
                label, confidence = predict_with_model(clf, emb)
                predicted_id = int(label)
            except Exception:
                predicted_id, confidence = None, 0.0
        x1, y1, x2, y2 = [int(v) for v in box]
        crop = img[y1:y2, x1:x2].copy() if x2 > x1 and y2 > y1 else None
        out.append({
            'bbox': [x1, y1, x2, y2],
            'embedding': emb,
            'predicted_student_id': predicted_id,
            'confidence': float(confidence or 0.0),
            'crop': crop,
        })
    return out


def _face_upsert_detection(conn, job_id, face_key, kind, student_id=None, student_name=None, reg_no=None,
                           confidence=0.0, samples=1, flagged=0, crop_bytes=None, bbox=None, frame_time=None,
                           assigned_by=None):
    c = conn.cursor()
    now = _now_iso()
    try:
        samples = max(1, int(samples))
    except Exception:
        samples = 1
    confidence = float(confidence or 0.0)
    row = c.execute(
        'SELECT id,sample_count,match_confidence,crop_path,flagged FROM face_attendance_faces WHERE job_id=? AND face_key=?',
        (job_id, face_key)
    ).fetchone()
    if row:
        face_id, old_count, old_conf, old_crop, old_flag = row
        total = int(old_count or 1) + samples
        use_new_crop = bool(crop_bytes) and confidence >= float(old_conf or 0.0)
        crop_path = old_crop
        if use_new_crop:
            new_crop = _face_save_crop(job_id, face_key, crop_bytes)
            if new_crop:
                crop_path = new_crop
                if old_crop:
                    _face_remove_path(old_crop)
        best_conf = max(float(old_conf or 0.0), confidence)
        final_flag = 1 if (int(old_flag or 0) or int(flagged or 0)) else 0
        c.execute(
            """UPDATE face_attendance_faces SET kind=?, student_id=?, student_name=?, reg_no=?, match_confidence=?,
                      sample_count=?, flagged=?, bbox=COALESCE(?,bbox), frame_time=COALESCE(?,frame_time),
                      crop_path=COALESCE(?,crop_path), assigned_by=COALESCE(?,assigned_by), assigned_at=?, updated_at=?
               WHERE id=?""",
            (kind, student_id, student_name, reg_no, best_conf, total, final_flag,
             json.dumps(bbox) if bbox else None, frame_time, crop_path, assigned_by,
             now if assigned_by else None, now, face_id)
        )
        return face_id
    crop_path = _face_save_crop(job_id, face_key, crop_bytes)
    cur = c.execute(
        """INSERT INTO face_attendance_faces
           (job_id,face_key,kind,student_id,student_name,reg_no,match_confidence,sample_count,flagged,bbox,frame_time,
            crop_path,assigned_by,assigned_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, face_key, kind, student_id, student_name, reg_no, confidence, samples, int(flagged or 0),
         json.dumps(bbox) if bbox else None, frame_time, crop_path, assigned_by,
         now if assigned_by else None, now, now)
    )
    return cur.lastrowid


def _face_delete_video(job):
    if not job or not job.get('video_path'):
        return
    _face_remove_path(job.get('video_path'), roots=(FACE_VIDEO_DIR,))
    conn = _db(); c = conn.cursor()
    c.execute('UPDATE face_attendance_jobs SET video_deleted=1 WHERE id=?', (job['id'],))
    conn.commit(); conn.close()


def _face_expire_videos():
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=FACE_JOB_VIDEO_TTL_HOURS)).isoformat()
    conn = _db(); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM face_attendance_jobs
           WHERE video_path IS NOT NULL AND COALESCE(video_deleted,0)=0 AND created_at<?
             AND status IN ('ready','failed','completed')""",
        (cutoff,)
    ).fetchall()
    conn.close()
    for row in rows:
        _face_delete_video(dict(row))


def _face_recover_interrupted_jobs():
    conn = _db(); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM face_attendance_jobs WHERE status IN ('queued','processing')").fetchall()
    conn.close()
    for row in rows:
        job = dict(row)
        _face_remove_path(job.get('video_path'), roots=(FACE_VIDEO_DIR,))
        conn = _db(); c = conn.cursor()
        c.execute("UPDATE face_attendance_jobs SET status='failed', error=?, finished_at=?, video_deleted=? WHERE id=?",
                  ('Server restarted before face video processing finished.', _now_iso(), 1 if job.get('video_path') else 0, job['id']))
        conn.commit(); conn.close()


def _process_face_video_job(job_id):
    if not _face_job_lock.acquire(blocking=False):
        threading.Thread(target=_process_face_video_job, args=(job_id,), daemon=True).start()
        return
    try:
        job = _face_job(job_id)
        if not job or job.get('status') in ('completed', 'failed'):
            return
        video_abs = _face_abs(job.get('video_path'))
        if not video_abs or not os.path.isfile(video_abs):
            _face_fail_job(job_id, 'Uploaded video file was not found.', delete_video=True)
            return
        conn = _db(); c = conn.cursor()
        c.execute("UPDATE face_attendance_jobs SET status='processing', progress=1, started_at=?, error=NULL WHERE id=?", (_now_iso(), job_id))
        conn.commit(); conn.close()

        roster = _face_roster_map(job['class_name'])
        cap = cv2.VideoCapture(video_abs)
        if not cap or not cap.isOpened():
            _face_fail_job(job_id, 'Could not open the uploaded video.', delete_video=True)
            return
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        sample_fps = max(0.2, FACE_VIDEO_SAMPLE_FPS)
        stride = max(1, int(round(fps / sample_fps))) if fps > 0 else 15
        limit_frames = int(FACE_VIDEO_MAX_SECONDS * fps) if fps > 0 else 0

        known = {}
        unknown_clusters = []
        unknown_matrix = None
        frames_scanned = 0
        faces_detected = 0
        last_update = time.time()
        truncated = False

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
            if limit_frames and frame_index > limit_frames:
                truncated = True
                break
            if frame_index % stride != 0:
                continue
            frames_scanned += 1
            height, width = frame.shape[:2]
            scale = 1.0
            scan_frame = frame
            if width > 1280:
                scale = 1280.0 / width
                scan_frame = cv2.resize(frame, (1280, int(height * scale)), interpolation=cv2.INTER_AREA)
            try:
                ok, buf = cv2.imencode('.jpg', scan_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                detections = _recognize_faces_image(buf.tobytes(), max_faces=FACE_VIDEO_MAX_FACES) if ok else []
            except Exception:
                detections = []
            frame_time = (frame_index / fps) if fps > 0 else None
            for det in detections:
                faces_detected += 1
                sid = det.get('predicted_student_id')
                conf = float(det.get('confidence') or 0.0)
                bbox = [int(v / scale) for v in det.get('bbox') or []]
                if sid in roster and conf >= FACE_MATCH_MIN_CONFIDENCE:
                    rec = known.get(sid)
                    if rec is None:
                        rec = {'samples': 0, 'conf': 0.0, 'crop': None, 'bbox': bbox, 'frame_time': frame_time,
                               'flagged': 0}
                        known[sid] = rec
                    rec['samples'] += 1
                    if conf >= rec['conf']:
                        rec['conf'] = conf
                        rec['crop'] = det.get('crop')
                        rec['bbox'] = bbox
                        rec['frame_time'] = frame_time
                    rec['flagged'] = 1 if rec['conf'] < FACE_MATCH_REVIEW_CONFIDENCE else 0
                else:
                    emb = det.get('embedding')
                    candidate = None
                    if emb is not None:
                        candidate = np.asarray(emb, dtype=np.float32)
                        norm = float(np.linalg.norm(candidate))
                        candidate = candidate / norm if norm > 0 else None
                    best = None
                    best_sim = 0.0
                    best_index = -1
                    if candidate is not None and unknown_clusters:
                        matrix_ok = (
                            isinstance(unknown_matrix, np.ndarray)
                            and unknown_matrix.shape[0] == len(unknown_clusters)
                            and unknown_matrix.shape[1] == candidate.size
                        )
                        if not matrix_ok:
                            rows = []
                            for cluster in unknown_clusters:
                                vec = cluster.get('embedding')
                                if vec is None or np.asarray(vec).size != candidate.size:
                                    rows.append(np.zeros(candidate.size, dtype=np.float32))
                                else:
                                    rows.append(np.asarray(vec, dtype=np.float32))
                            unknown_matrix = np.vstack(rows)
                        sims = unknown_matrix @ candidate
                        best_index = int(np.argmax(sims))
                        best_sim = float(sims[best_index])
                        best = unknown_clusters[best_index]
                    if best is not None and best_sim >= FACE_UNKNOWN_SIMILARITY:
                        best['samples'] += 1
                        if conf >= best['conf']:
                            best['conf'] = conf
                            best['crop'] = det.get('crop')
                            best['bbox'] = bbox
                            best['frame_time'] = frame_time
                        if candidate is not None:
                            best['embedding'] = candidate
                            matrix_ok = (
                                isinstance(unknown_matrix, np.ndarray)
                                and unknown_matrix.shape[0] == len(unknown_clusters)
                                and unknown_matrix.shape[1] == candidate.size
                            )
                            if matrix_ok:
                                unknown_matrix[best_index] = candidate
                            else:
                                unknown_matrix = None
                    elif len(unknown_clusters) < FACE_UNKNOWN_MAX_GROUPS:
                        unknown_clusters.append({'samples': 1, 'conf': conf, 'crop': det.get('crop'), 'bbox': bbox,
                                                 'frame_time': frame_time, 'embedding': candidate})
                        if candidate is not None:
                            if (isinstance(unknown_matrix, np.ndarray)
                                    and unknown_matrix.shape[0] == len(unknown_clusters) - 1
                                    and unknown_matrix.shape[1] == candidate.size):
                                unknown_matrix = np.vstack([unknown_matrix, candidate.reshape(1, -1)])
                            else:
                                unknown_matrix = None
            if time.time() - last_update >= 2.0:
                if total_frames:
                    progress = min(99, max(1, int((frame_index / float(total_frames)) * 100)))
                elif limit_frames:
                    progress = min(99, max(1, int((frame_index / float(limit_frames)) * 100)))
                else:
                    progress = min(99, 1 + frames_scanned // 20)
                conn = _db(); c = conn.cursor()
                c.execute("""UPDATE face_attendance_jobs SET progress=?, frames_scanned=?, faces_detected=?,
                                     matched_students=?, unknown_faces=? WHERE id=?""",
                          (progress, frames_scanned, faces_detected, len(known), len(unknown_clusters), job_id))
                conn.commit(); conn.close()
                last_update = time.time()
        cap.release()

        conn = _db(); c = conn.cursor()
        for sid, rec in known.items():
            info = roster.get(sid) or {}
            _face_upsert_detection(conn, job_id, f'student:{sid}', 'student', sid, info.get('name'), info.get('reg_no'),
                                   rec['conf'], rec['samples'], rec['flagged'], _face_encode_crop(rec['crop']),
                                   rec['bbox'], rec['frame_time'])
        unknown_clusters.sort(key=lambda x: x.get('samples', 0), reverse=True)
        for idx, rec in enumerate(unknown_clusters[:200], start=1):
            _face_upsert_detection(conn, job_id, f'unknown:{idx}', 'unknown', None, None, None,
                                   rec['conf'], rec['samples'], 1, _face_encode_crop(rec['crop']),
                                   rec['bbox'], rec['frame_time'])
        matched, unknown = _face_refresh_counts(c, job_id)
        notes = f'Processed at {FACE_VIDEO_SAMPLE_FPS:g} fps' + (f'; stopped at {FACE_VIDEO_MAX_SECONDS}s cap.' if truncated else '.')
        c.execute("""UPDATE face_attendance_jobs SET status='ready', progress=100, frames_scanned=?, faces_detected=?,
                             matched_students=?, unknown_faces=?, notes=?, finished_at=? WHERE id=?""",
                  (frames_scanned, faces_detected, matched, unknown, notes, _now_iso(), job_id))
        conn.commit(); conn.close()
    except Exception as exc:
        _face_fail_job(job_id, f'Face video processing failed: {exc}', delete_video=True)
    finally:
        _face_job_lock.release()


def _face_fail_job(job_id, error, delete_video=True):
    job = _face_job(job_id)
    if delete_video and job:
        _face_delete_video(job)
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE face_attendance_jobs SET status='failed', error=?, progress=100, finished_at=? WHERE id=?",
              (error, _now_iso(), job_id))
    conn.commit(); conn.close()


def _face_client_job(job_id):
    job = _face_job_payload(job_id)
    if job:
        job.pop('video_path', None)
    return job


@app.route('/admin/face_attendance')
@admin_required
def admin_face_attendance_page():
    cfg = {
        'maxVideoMb': FACE_VIDEO_MAX_MB,
        'maxWebcamMb': FACE_WEBCAM_MAX_MB,
        'sampleFps': FACE_VIDEO_SAMPLE_FPS,
        'maxVideoFaces': FACE_VIDEO_MAX_FACES,
        'maxUnknownGroups': FACE_UNKNOWN_MAX_GROUPS,
        'minConfidence': FACE_MATCH_MIN_CONFIDENCE,
        'reviewConfidence': FACE_MATCH_REVIEW_CONFIDENCE,
        'allowedVideo': sorted(ALLOWED_VIDEO_EXTENSIONS),
    }
    return render_template('admin_face_attendance.html', user=_current_user(), classes=get_classes(),
                           today=_today_ist_str(), face_cfg=cfg)


@app.route('/api/admin/face/status')
@admin_required
def api_admin_face_status():
    _face_expire_videos()
    model_ready = os.path.exists(MODEL_PATH if os.path.isabs(MODEL_PATH) else os.path.join(APP_DIR, MODEL_PATH))
    conn = _db(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM face_attendance_jobs WHERE status IN ('queued','processing')")
    active = int(c.fetchone()[0] or 0)
    conn.close()
    models = face_capabilities()
    return jsonify(ok=True, model_ready=model_ready, train=read_train_status(), active_jobs=active,
                   models=models,
                   thresholds={'match': FACE_MATCH_MIN_CONFIDENCE, 'review': FACE_MATCH_REVIEW_CONFIDENCE},
                   limits={'video_mb': FACE_VIDEO_MAX_MB, 'webcam_mb': FACE_WEBCAM_MAX_MB,
                           'video_faces': FACE_VIDEO_MAX_FACES, 'unknown_groups': FACE_UNKNOWN_MAX_GROUPS},
                   sample_fps=FACE_VIDEO_SAMPLE_FPS)


@app.route('/api/admin/face/jobs', methods=['GET'])
@admin_required
def api_admin_face_jobs():
    _face_expire_videos()
    conn = _db(); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM face_attendance_jobs ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    jobs = []
    for row in rows:
        item = dict(row)
        item.pop('video_path', None)
        jobs.append(item)
    return jsonify(ok=True, jobs=jobs)


@app.route('/api/admin/face/jobs', methods=['POST'])
@admin_required
def api_admin_face_job_create():
    ctx, error = _face_validate_context(request.form.get('class'), request.form.get('date'), request.form.get('period'))
    if error:
        return jsonify(ok=False, error=error), 400
    file = request.files.get('video')
    if not file or not file.filename:
        return jsonify(ok=False, error='Choose a video file to upload.'), 400
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify(ok=False, error=f"Unsupported format. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}"), 400
    save_name = f"{ctx['class_name']}_{ctx['date']}_P{ctx['period']}_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    save_path = os.path.join(FACE_VIDEO_DIR, save_name)
    try:
        file.save(save_path)
        size = os.path.getsize(save_path)
    except Exception as exc:
        return jsonify(ok=False, error=f'Could not save video: {exc}'), 500
    if size <= 0:
        _face_remove_path(_face_rel(save_path), roots=(FACE_VIDEO_DIR,))
        return jsonify(ok=False, error='Uploaded video is empty.'), 400
    if size > FACE_VIDEO_MAX_MB * 1024 * 1024:
        _face_remove_path(_face_rel(save_path), roots=(FACE_VIDEO_DIR,))
        return jsonify(ok=False, error=f'Video exceeds {FACE_VIDEO_MAX_MB} MB.'), 400
    conn = _db(); c = conn.cursor()
    c.execute("""INSERT INTO face_attendance_jobs
                 (source,class_name,date,period,status,progress,video_path,video_size,created_by,created_at)
                 VALUES ('video',?,?,?, 'queued',0,?,?,?,?)""",
              (ctx['class_name'], ctx['date'], ctx['period'], _face_rel(save_path), size,
               session.get('display_name') or session.get('username'), _now_iso()))
    job_id = c.lastrowid
    conn.commit(); conn.close()
    audit_log('face_attendance_job', job_id, 'create', None,
              {'source': 'video', 'class': ctx['class_name'], 'date': ctx['date'], 'period': ctx['period'], 'size': size},
              details='Admin uploaded attendance video for local face processing')
    payload = _face_client_job(job_id)
    threading.Thread(target=_process_face_video_job, args=(job_id,), daemon=True).start()
    return jsonify(ok=True, job=payload), 202


@app.route('/api/admin/face/jobs/<int:job_id>', methods=['GET'])
@admin_required
def api_admin_face_job_get(job_id):
    _face_expire_videos()
    job = _face_client_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    return jsonify(ok=True, job=job)


@app.route('/api/admin/face/jobs/<int:job_id>', methods=['DELETE'])
@admin_required
def api_admin_face_job_delete(job_id):
    job = _face_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    if job.get('status') in ('queued', 'processing', 'recording'):
        return jsonify(ok=False, error='Stop or wait for this job before deleting it.'), 409
    _face_delete_video(job)
    folder = os.path.join(FACE_CROP_DIR, f'job_{int(job_id)}')
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    conn = _db(); c = conn.cursor()
    c.execute('DELETE FROM face_attendance_faces WHERE job_id=?', (job_id,))
    c.execute('DELETE FROM face_attendance_jobs WHERE id=?', (job_id,))
    conn.commit(); conn.close()
    audit_log('face_attendance_job', job_id, 'delete', {'class': job.get('class_name'), 'date': job.get('date'), 'period': job.get('period')}, None,
              details='Admin deleted face attendance job and derived crops')
    return jsonify(ok=True)


@app.route('/api/admin/face/webcam/start', methods=['POST'])
@admin_required
def api_admin_face_webcam_start():
    data = request.get_json(silent=True) or {}
    ctx, error = _face_validate_context(data.get('class'), data.get('date'), data.get('period'))
    if error:
        return jsonify(ok=False, error=error), 400
    conn = _db(); c = conn.cursor()
    c.execute("""INSERT INTO face_attendance_jobs
                 (source,class_name,date,period,status,progress,created_by,created_at,started_at)
                 VALUES ('webcam',?,?,?, 'recording',0,?,?,?)""",
              (ctx['class_name'], ctx['date'], ctx['period'], session.get('display_name') or session.get('username'),
               _now_iso(), _now_iso()))
    job_id = c.lastrowid
    conn.commit(); conn.close()
    audit_log('face_attendance_job', job_id, 'start', None,
              {'source': 'webcam', 'class': ctx['class_name'], 'date': ctx['date'], 'period': ctx['period']},
              details='Admin started local webcam face attendance')
    return jsonify(ok=True, job=_face_client_job(job_id))


def _face_autoscan_mark(job, sid, conf):
    cls=(job.get('class_name') or '').strip(); date=(job.get('date') or '').strip(); period=job.get('period')
    try: period=int(period)
    except Exception: return False, 'Invalid period'
    ok,reason=can_mark_period(cls,date,period)
    if not ok: return False, reason
    cell=timetable_cell(cls,date,period) or {}
    if not cell: return False, 'No current timetable slot'
    edit_allowed, edit_reason = attendance_edit_allowed(date)
    if not edit_allowed: return False, edit_reason
    conn=_db(); c=conn.cursor()
    try:
        c.execute('SELECT id,name,reg_no,class,student_role FROM students WHERE id=? AND upper(class)=upper(?) AND (active IS NULL OR active=1)',(sid,cls))
        st=c.fetchone()
        if not st: return False, 'Student not in class'
        _, sname, reg, scls, _srole = st
        disp=(cell.get('display') or cell.get('subject') or '').strip(); subj=(cell.get('subject') or disp).strip()
        version=_timetable_version_row(cls,date); version_id=int(version['id']) if version else None
        tt=load_timetable(cls,date) or {}; duration=_slot_duration_minutes(tt,period)
        marked_by=session.get('display_name') or session.get('username') or 'admin'; now=_now_iso()
        c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,date,period,disp))
        row=c.fetchone()
        if row:
            sess_id=row[0]
            c.execute('UPDATE attendance_sessions SET subject=?,subject_display=?,staff_code=?,staff_name=?,status=?,source=?,submitted_at=?,timetable_version_id=?,duration_minutes=? WHERE id=?',
                      (subj,disp,session.get('staff_code'),marked_by,'submitted','face_attendance',now,version_id,duration,sess_id))
        else:
            c.execute('INSERT INTO attendance_sessions (class_name,date,period,subject,subject_display,staff_code,staff_name,status,source,created_at,submitted_at,notes,timetable_version_id,duration_minutes) VALUES (?,?,?,?,?,?,?, "submitted",?,?,?,?,?,?)',
                      (cls,date,period,subj,disp,session.get('staff_code'),marked_by,'face_attendance',now,now,'Auto-scan face attendance',version_id,duration))
            sess_id=c.lastrowid
        # locks
        od_approved=False; medical_approved=False
        try:
            c.execute("SELECT 1 FROM od_records WHERE student_id=? AND class_name=? AND date=? AND period=? AND status='OD' LIMIT 1",(sid,cls,date,period))
            od_approved=bool(c.fetchone())
        except Exception: pass
        try:
            c.execute("SELECT 1 FROM medical_records WHERE student_id=? AND class_name=? AND date=? AND period=? AND status='Medical Leave' LIMIT 1",(sid,cls,date,period))
            medical_approved=bool(c.fetchone())
        except Exception: pass
        c.execute('SELECT status,source,elective_label FROM attendance_entries WHERE session_id=? AND student_id=?',(sess_id,sid))
        prev=c.fetchone(); prev_st=_normalize_attendance_status(prev[0] if prev else None); prev_src=(prev[1] if prev else '')
        if od_approved or (prev_st=='OD' and prev_src=='od_workflow'): return False, 'OD locked'
        if medical_approved or (prev_st=='Medical Leave' and prev_src=='medical_workflow'): return False, 'Medical Leave locked'
        if prev_st!=UNMARKED_STATUS and not edit_allowed: return False, edit_reason
        elabel=''
        if cell.get('options'):
            emaps=_elective_lookup(); elabel=_effective_subject(emaps,sid,cls,date,period,prev[2] if prev else '',version_id)
            if session.get('role')=='staff' and not (session.get('is_hod') or session.get('role')=='admin'):
                allowed=_elective_allowed_staff_codes(((_elective_offering_map(cls).get(_period_slot_key(period,date)) or {})), elabel, cell)
                if allowed and (session.get('username','').upper() not in allowed): return False, 'Mapped elective staff only'
        c.execute("INSERT INTO attendance_entries (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at,elective_label) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET reg_no=excluded.reg_no,student_name=excluded.student_name,status=excluded.status,confidence=excluded.confidence,flagged=excluded.flagged,source=excluded.source,marked_by=excluded.marked_by,updated_at=excluded.updated_at,elective_label=COALESCE(NULLIF(excluded.elective_label,''),attendance_entries.elective_label)",
                  (sess_id,sid,reg,sname,'Present',max(0.0,min(1.0,float(conf or 0))),0,'face_attendance',marked_by,now,elabel))
        conn.commit()
        try:
            _enqueue_alert(sid, 'attendance_marked', 'Attendance marked', f'Present: {cls} {date} P{period} {disp}'.strip(), event_key='face_auto', source_ref=f'face_auto:{sess_id}:{sid}')
        except Exception: pass
        return True, 'marked'
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return False, str(e)
    finally:
        try: conn.close()
        except Exception: pass

@app.route('/api/admin/face/jobs/<int:job_id>/frame', methods=['POST'])
@admin_required
def api_admin_face_job_frame(job_id):
    job = _face_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    if job.get('source') != 'webcam':
        return jsonify(ok=False, error='Frame capture is only available for webcam jobs.'), 400
    if job.get('status') != 'recording':
        return jsonify(ok=False, error='Start the webcam job before capturing frames.'), 400
    file = request.files.get('image')
    data = file.read() if file else b''
    if not data:
        return jsonify(ok=False, error='No webcam image received.'), 400
    if len(data) > FACE_WEBCAM_MAX_MB * 1024 * 1024:
        return jsonify(ok=False, error=f'Webcam image exceeds {FACE_WEBCAM_MAX_MB} MB.'), 400
    autoscan = str(request.form.get('autoscan') or request.args.get('autoscan') or '').lower() in ('1','true','yes','on')
    detections = _recognize_faces_image(data, max_faces=(FACE_VIDEO_MAX_FACES if autoscan else 1))
    roster = _face_roster_map(job['class_name'])
    conn = _db(); c = conn.cursor()
    auto_marked=[]; auto_skipped=[]
    now_ts=time.time()
    state=_face_autoscan_state.setdefault(int(job_id),{})
    seen=set(); det_conf={}
    for det in detections:
        sid = det.get('predicted_student_id')
        conf = float(det.get('confidence') or 0.0)
        if sid in roster and conf >= FACE_MATCH_MIN_CONFIDENCE:
            info = roster[sid]
            _face_upsert_detection(conn, job_id, f'student:{sid}', 'student', sid, info.get('name'), info.get('reg_no'),
                                   conf, 1, 1 if conf < FACE_MATCH_REVIEW_CONFIDENCE else 0,
                                   _face_encode_crop(det.get('crop')), det.get('bbox'), None)
            if autoscan:
                sid_i=int(sid); seen.add(sid_i); det_conf[sid_i]=max(det_conf.get(sid_i,0.0), conf)
        else:
            _face_upsert_detection(conn, job_id, f'unknown:webcam:{int(now_ts*1000)}', 'unknown', None, None, None,
                                   conf, 1, 1, _face_encode_crop(det.get('crop')), det.get('bbox'), None)
    if autoscan:
        for sid_k, st in list(state.items()):
            if sid_k not in seen:
                st['streak']=0
        for sid in seen:
            st=state.setdefault(sid, {'streak':0,'last_mark_ts':0,'last_seen_ts':0})
            st['streak']=int(st.get('streak',0))+1; st['last_seen_ts']=now_ts
            if st['streak']>=FACE_AUTOSCAN_STABLE and (now_ts-float(st.get('last_mark_ts',0) or 0))>=FACE_AUTOSCAN_COOLDOWN_SEC:
                ok_m, msg_m = _face_autoscan_mark(job, sid, det_conf.get(sid, 0.0))
                if ok_m:
                    st['last_mark_ts']=now_ts; st['streak']=0
                    auto_marked.append({'student_id':sid,'name':roster.get(sid,{}).get('name'),'reg_no':roster.get(sid,{}).get('reg_no')})
                else:
                    auto_skipped.append({'student_id':sid,'reason':msg_m})
    _face_refresh_counts(c, job_id)
    c.execute("""UPDATE face_attendance_jobs SET frames_scanned=COALESCE(frames_scanned,0)+1,
                        faces_detected=COALESCE(faces_detected,0)+? WHERE id=?""",
              (len(detections), job_id))
    conn.commit(); conn.close()
    return jsonify(ok=True, detected=len(detections), job=_face_client_job(job_id),
                   auto_marked=auto_marked, auto_skipped=auto_skipped, autoscan=autoscan,
                   warning=None if detections else 'No face detected. Ask the student to face the camera and retry.')


@app.route('/api/admin/face/jobs/<int:job_id>/stop', methods=['POST'])
@admin_required
def api_admin_face_job_stop(job_id):
    job = _face_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    if job.get('source') != 'webcam':
        return jsonify(ok=False, error='Only webcam jobs can be stopped this way.'), 400
    if job.get('status') == 'completed':
        return jsonify(ok=True, job=_face_client_job(job_id))
    conn = _db(); c = conn.cursor()
    c.execute("UPDATE face_attendance_jobs SET status='ready', progress=100, finished_at=? WHERE id=?", (_now_iso(), job_id))
    conn.commit(); conn.close()
    audit_log('face_attendance_job', job_id, 'stop', None, {'status': 'ready'},
              details='Admin stopped webcam face attendance capture')
    return jsonify(ok=True, job=_face_client_job(job_id))


@app.route('/api/admin/face/jobs/<int:job_id>/assign', methods=['POST'])
@admin_required
def api_admin_face_job_assign(job_id):
    job = _face_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    if job.get('status') in ('queued', 'processing') or (job.get('status') == 'recording' and job.get('source') != 'webcam'):
        return jsonify(ok=False, error='Wait for processing to finish before assigning faces.'), 409
    data = request.get_json(silent=True) or {}
    try:
        face_id = int(data.get('face_id')); student_id = int(data.get('student_id'))
    except Exception:
        return jsonify(ok=False, error='Select a face and a student.'), 400
    roster = _face_roster_map(job['class_name'])
    if student_id not in roster:
        return jsonify(ok=False, error='Student is not in the selected class.'), 400
    conn = _db(); conn.row_factory = sqlite3.Row
    face = conn.execute('SELECT * FROM face_attendance_faces WHERE id=? AND job_id=?', (face_id, job_id)).fetchone()
    if not face:
        conn.close()
        return jsonify(ok=False, error='Face not found for this job.'), 404
    face = dict(face)
    info = roster[student_id]
    target_key = f'student:{student_id}'
    target = conn.execute('SELECT * FROM face_attendance_faces WHERE job_id=? AND face_key=?', (job_id, target_key)).fetchone()
    if face.get('face_key') == target_key:
        conn.close()
        return jsonify(ok=True, job=_face_client_job(job_id))
    now = _now_iso()
    c = conn.cursor()
    if target and target['id'] != face_id:
        total = int(target['sample_count'] or 1) + int(face['sample_count'] or 1)
        keep_crop = target['crop_path'] if float(target['match_confidence'] or 0) >= float(face['match_confidence'] or 0) else face['crop_path']
        if keep_crop == face['crop_path'] and target['crop_path'] and target['crop_path'] != face['crop_path']:
            _face_remove_path(target['crop_path'])
        c.execute("""UPDATE face_attendance_faces SET sample_count=?, flagged=0, crop_path=?, assigned_by=?, assigned_at=?, updated_at=?
                     WHERE id=?""",
                  (total, keep_crop, session.get('display_name') or session.get('username'), now, now, target['id']))
        c.execute('DELETE FROM face_attendance_faces WHERE id=?', (face_id,))
        if keep_crop != face['crop_path'] and face['crop_path']:
            _face_remove_path(face['crop_path'])
    else:
        c.execute("""UPDATE face_attendance_faces SET face_key=?, kind='student', student_id=?, student_name=?, reg_no=?,
                            flagged=0, assigned_by=?, assigned_at=?, updated_at=? WHERE id=?""",
                  (target_key, student_id, info.get('name'), info.get('reg_no'),
                   session.get('display_name') or session.get('username'), now, now, face_id))
    _face_refresh_counts(c, job_id)
    conn.commit(); conn.close()
    audit_log('face_attendance_job', job_id, 'assign_face', {'face_id': face_id, 'kind': face.get('kind')},
              {'student_id': student_id, 'name': info.get('name')}, details='Admin manually assigned/override a face match')
    return jsonify(ok=True, job=_face_client_job(job_id))


@app.route('/api/admin/face/jobs/<int:job_id>/complete', methods=['POST'])
@admin_required
def api_admin_face_job_complete(job_id):
    job = _face_job(job_id)
    if not job:
        return jsonify(ok=False, error='Job not found.'), 404
    if job.get('status') in ('queued', 'processing', 'recording'):
        return jsonify(ok=False, error='Stop capture and wait for processing before completing this job.'), 409
    if job.get('status') != 'completed':
        _face_delete_video(job)
        conn = _db(); c = conn.cursor()
        c.execute("UPDATE face_attendance_jobs SET status='completed', reviewed_at=?, progress=100 WHERE id=?", (_now_iso(), job_id))
        conn.commit(); conn.close()
        audit_log('face_attendance_job', job_id, 'complete', {'status': job.get('status')}, {'status': 'completed'},
                  details='Admin completed face attendance review; uploaded video deleted')
    return jsonify(ok=True, job=_face_client_job(job_id))


@app.route('/api/admin/face/crops/<int:face_id>', methods=['GET'])
@admin_required
def api_admin_face_crop(face_id):
    conn = _db(); conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT crop_path FROM face_attendance_faces WHERE id=?', (face_id,)).fetchone()
    conn.close()
    if not row or not row['crop_path']:
        abort(404)
    path = _face_abs(row['crop_path'])
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='image/jpeg')


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
    cctv_running=False; cctv_stop_reason='Auto-marking disabled in ERP prototype. Use draft attendance and submit session flow.'
    return jsonify({'status':'disabled','message':cctv_stop_reason}), 410

@app.route('/cctv_status', methods=['GET'])
@staff_required
def cctv_status_disabled():
    return jsonify({'running':False,'message':'Auto-marking disabled in ERP prototype. Use draft attendance and submit session flow.','source_type':current_source_type,'source':str(current_source)})

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

def _report_period_bounds(period):
    today=_now_ist().date()
    if period=='daily': return today.isoformat(), today.isoformat()
    if period=='weekly': return (today-datetime.timedelta(days=6)).isoformat(), today.isoformat()
    if period=='monthly': return (today-datetime.timedelta(days=29)).isoformat(), today.isoformat()
    return '', ''

def _clean_date_pair(date_from='', date_to=''):
    d1=_parse_iso_date(date_from); d2=_parse_iso_date(date_to)
    if d1 and d2 and d2<d1: return '', ''
    return (d1.isoformat() if d1 else ''), (d2.isoformat() if d2 else '')

def _report_date_range(period, date_from='', date_to=''):
    dfrom,dto=_clean_date_pair(date_from,date_to)
    if not dfrom and not dto: return _report_period_bounds(period)
    return dfrom,dto

def _staff_scope_where():
    if session.get('role')=='admin': return '', []
    code=(session.get('staff_code') or session.get('username') or '').strip()
    name=(session.get('display_name') or '').strip()
    return "(upper(COALESCE(s.staff_code,''))=upper(?) OR upper(COALESCE(s.staff_name,''))=upper(?))", [code, name]

def _report_absence_flags(items):
    by_day={}
    for item in items:
        key=item.get('status_key')
        if key=='unmarked': continue
        by_day.setdefault((item.get('student_id'), item.get('date')), []).append(key)
    full_days=set(); partial_days=set(); absent_days=set()
    for day_key,statuses in by_day.items():
        absent_count=sum(1 for key in statuses if key=='absent')
        if absent_count:
            absent_days.add(day_key)
            if absent_count==len(statuses): full_days.add(day_key)
            else: partial_days.add(day_key)
    return full_days, partial_days, absent_days

def _report_absence_keep(item, absence, full_days, partial_days, absent_days):
    day_key=(item.get('student_id'), item.get('date'))
    if absence=='full_day': return day_key in full_days
    if absence=='partial_day': return day_key in partial_days
    if absence=='period_wise': return item.get('status_key')=='absent'
    if absence=='none': return day_key not in absent_days
    return True

@app.route('/attendance_record', methods=['GET'])
@staff_required
def attendance_record():
    period=request.args.get('period','all'); cls=(request.args.get('class') or '').strip()
    dfrom,dto=_clean_date_pair(request.args.get('from'),request.args.get('to'))
    absence=(request.args.get('absence') or 'any').strip().lower()
    if absence not in ('any','full_day','partial_day','period_wise','none'): absence='any'
    sem_id=(request.args.get('semester_id') or '').strip()
    sem=_semester_by_id(sem_id) if sem_id else None
    sem_no_raw=request.args.get('sem_no'); by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,'') and by_raw not in (None,''):
        try: sem=_batch_semester_row(int(by_raw),int(sem_no_raw))
        except Exception: sem=None
    if sem and not dfrom and not dto:
        sd1,sd2=_semester_window(sem, cls or None); report_from=sd1.isoformat(); report_to=sd2.isoformat()
    else:
        report_from,report_to=_report_date_range(period,dfrom,dto)
    conn=_db(); c=conn.cursor()
    where=[]; params=[]
    if cls: where.append('upper(s.class_name)=upper(?)'); params.append(cls)
    if report_from: where.append('s.date>=?'); params.append(report_from)
    if report_to: where.append('s.date<=?'); params.append(report_to)
    scope_clause,scope_params=_staff_scope_where()
    if scope_clause: where.append(scope_clause); params += scope_params
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    q = ("SELECT s.date,s.class_name,s.period,s.subject_display,e.student_name,e.reg_no,e.status,e.marked_by,e.updated_at,s.id,e.student_id,e.elective_label "
         "FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"+wh+" ORDER BY s.date DESC,s.period DESC,s.class_name,e.student_name LIMIT 5000")
    try:
        c.execute(q, params); session_rows=c.fetchall()
        emaps=_elective_lookup()
        session_rows=[tuple(r)+((r[11] or _effective_subject(emaps,r[10],r[1],r[0],r[2],'')), _analytics_status_key(r[6])) for r in session_rows]
    except Exception: session_rows=[]
    conn.close()
    by_day={}
    for r in session_rows:
        st_key=_analytics_status_key(r[6])
        if st_key=='unmarked': continue
        by_day.setdefault((r[10],r[0]),[]).append(st_key)
    full_days=set(); partial_days=set(); absent_days=set()
    for day_key,statuses in by_day.items():
        absent_count=sum(1 for s in statuses if s=='absent')
        if absent_count:
            absent_days.add(day_key)
            if absent_count==len(statuses): full_days.add(day_key)
            else: partial_days.add(day_key)
    def _keep_row(r):
        day_key=(r[10],r[0])
        if absence=='full_day': return day_key in full_days
        if absence=='partial_day': return day_key in partial_days
        if absence=='period_wise': return _analytics_status_key(r[6])=='absent'
        if absence=='none': return day_key not in absent_days
        return True
    session_rows=[r for r in session_rows if _keep_row(r)]
    session_roles=_student_roles_map([r[10] for r in session_rows])
    agg={}
    for r in session_rows:
        st=_analytics_status_key(r[6])
        if st=='unmarked': continue
        key=(r[10],r[4],r[5],r[1])
        a=agg.setdefault(key,{'present':0,'absent':0,'total':0})
        a['total']+=1
        if st=='absent': a['absent']+=1
        elif st in ('present','late','od','medical'): a['present']+=1
    report_rows=[]
    total=present=absent=0
    for (sid,name,reg,cn),a in sorted(agg.items(), key=lambda x:((x[0][3] or ''),(x[0][1] or ''))):
        total+=1; pcount=a['present']; acount=a['absent']; tcount=a['total']
        if pcount>0: present+=1
        if acount>0 and pcount==0: absent+=1
        pct=round((pcount/tcount*100),1) if tcount else 0
        status='Present/OD' if pcount>0 else 'Absent'
        report_rows.append({'id':sid,'name':name,'reg_no':reg or '','class':cn or '','status':status,
                            'student_role':session_roles.get(int(sid or 0),''),
                            'present':pcount,'absent':acount,'total':tcount,'pct':pct,
                            'first_in_fmt':'','last_out_fmt':'','duration':'','roll':reg or '','section':'','manual':f'{pcount} credited, {acount} absent, {pct}%'} )
    note='Period-session manual attendance (submitted sessions). Unmarked "-" rows are excluded; N counts as absent. Changes/overrides update attendance entries and are reflected in reports/student log. Missing exit never marks absent.'
    if session.get('role')!='admin': note+=' Staff view covers only sessions handled/submitted by you.'
    role=session.get('role') or 'staff'
    by=None
    if by_raw not in (None,''):
        try: by=int(by_raw)
        except Exception: by=None
    if by is None and cls:
        info=parse_cohort_class(cls)
        if info: by=info.get('batch_start_year')
    today_s=_today_ist_str()
    if by is not None:
        ensure_batch_semesters(by, actor=session.get('username') or 'system')
        visible, _cur_no = visible_sem_nos(role, by, on_date=today_s)
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        sem_list=[_semester_json(x,batch_start_year=by,on_date=today_s) for x in sem_rows if role=='admin' or int(x.get('sem_no') or 0) in set(visible)]
    else:
        sem_list=[_semester_json(x,on_date=today_s) for x in _semester_rows()]
    batches=[{'batch_start_year':b,'label':batch_label(b)} for b in BATCH_START_YEARS]
    return render_template('attendance_record.html', session_rows=session_rows, session_roles=session_roles, period=period, class_name=cls, report_rows=report_rows,
                           semesters=sem_list, semester_id=(sem or {}).get('id'), absence=absence,
                           date_from=report_from, date_to=report_to, erp_classes=get_classes(),
                           summary={'total':total,'present':present,'absent':absent,'note':note},
                           batches=batches, batch_start_year=by, selected_sem_no=(int(sem_no_raw) if str(sem_no_raw or '').isdigit() else None))

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
    conn=_db(); c=conn.cursor(); logs=[]
    try:
        c.execute("SELECT s.date,s.period,s.subject_display,s.class_name,e.status,e.marked_by,e.updated_at,s.submitted_at FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id WHERE e.student_id=? ORDER BY s.date DESC, s.period DESC, e.updated_at DESC LIMIT 200", (student[0],))
        for r in c.fetchall():
            ts = r[6] or r[7] or (r[0]+'T00:00:00' if r[0] else '')
            status=_normalize_attendance_status(r[4])
            logs.append({'date':r[0],'period':r[1],'subject':r[2] or '','class':r[3] or '','status':status,'marked_by':r[5] or '', 'timestamp':ts,
                         'label': f"P{r[1]} {r[2] or ''} - {status}", 'action': status})
    except Exception: pass
    try:
        c.execute('SELECT student_name,action,timestamp FROM notifications WHERE student_id=? ORDER BY id DESC LIMIT 50',(student[0],))
        for r in c.fetchall(): logs.append({'label':str(r[1]), 'action':r[1], 'timestamp':r[2], 'legacy':True})
    except Exception: pass
    unread=0
    try:
        c.execute('SELECT id,alert_type,title,body,created_at,read_at FROM student_alerts WHERE student_id=? ORDER BY id DESC LIMIT 100',(student[0],))
        for r in c.fetchall():
            is_unread = not r[5]
            if is_unread: unread += 1
            logs.append({'alert_id':r[0],'type':r[1],'label':r[2],'title':r[2],'body':r[3],'message':r[3],
                         'timestamp':r[4],'read':bool(r[5]),'action':r[2],'is_alert':True})
    except Exception: pass
    conn.close()
    logs=sorted(logs,key=lambda x:(x.get('timestamp') or ''),reverse=True)[:200]
    return jsonify({'notifications':logs,'attendance_log':logs,'unread':unread,'student':{'id':student[0],'name':student[1],'roll':student[2],'class':student[3],'section':student[4],'reg_no':student[5]}})

@app.route('/api/student/alerts/<int:alert_id>/read', methods=['POST'])
@student_required
def api_student_alert_read(alert_id):
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile'}), 404
    conn=_db(); c=conn.cursor(); c.execute('UPDATE student_alerts SET read_at=COALESCE(read_at,?) WHERE id=? AND student_id=?',(_now_iso(),alert_id,student[0])); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/student/alerts/read_all', methods=['POST'])
@student_required
def api_student_alerts_read_all():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile'}), 404
    conn=_db(); c=conn.cursor();     c.execute('UPDATE student_alerts SET read_at=? WHERE student_id=? AND read_at IS NULL',(_now_iso(),student[0])); changed=c.rowcount; conn.commit(); conn.close()
    return jsonify({'ok':True,'marked':changed})

def _staff_alert_access():
    if session.get('role')=='admin':
        return '', []
    code=_upper(session.get('staff_code') or session.get('username'))
    uid=session.get('user_id')
    if not code and not uid: return '1=0', []
    return "(upper(COALESCE(recipient_code,''))=upper(?) OR recipient_user_id=?)", [code, uid]

@app.route('/api/staff/alerts', methods=['GET'])
@app.route('/api/staff/unmarked_reminders', methods=['GET'])
@staff_required
def api_staff_alerts():
    where, params=_staff_alert_access()
    wh=(' WHERE '+where) if where else ''
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT id,recipient_code,title,body,source_ref,created_at,read_at
                 FROM staff_alerts"""+wh+" ORDER BY created_at DESC,id DESC LIMIT 100", params)
    rows=c.fetchall(); conn.close()
    alerts=[{'id':r[0],'recipient_code':r[1] or '', 'title':r[2] or '', 'body':r[3] or '',
             'source_ref':r[4] or '', 'created_at':r[5] or '', 'read':bool(r[6])} for r in rows]
    return jsonify({'ok':True,'alerts':alerts,'unread':sum(1 for a in alerts if not a['read'])})

@app.route('/api/staff/alerts/<int:alert_id>/read', methods=['POST'])
@staff_required
def api_staff_alert_read(alert_id):
    where, params=_staff_alert_access()
    wh=(' AND '+where) if where else ''
    conn=_db(); c=conn.cursor()
    c.execute("""UPDATE staff_alerts SET read_at=COALESCE(read_at,?)
                 WHERE id=?"""+wh, [_now_iso(),alert_id]+params)
    changed=c.rowcount; conn.commit(); conn.close()
    return jsonify({'ok':True,'marked':changed})

@app.route('/api/staff/alerts/read_all', methods=['POST'])
@staff_required
def api_staff_alerts_read_all():
    where, params=_staff_alert_access()
    wh=(' AND '+where) if where else ''
    conn=_db(); c=conn.cursor()
    c.execute("UPDATE staff_alerts SET read_at=? WHERE read_at IS NULL"+wh, [_now_iso()]+params)
    changed=c.rowcount; conn.commit(); conn.close()
    return jsonify({'ok':True,'marked':changed})

@app.route('/student_summary', methods=['GET'])
@student_required
def student_summary():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile found.'}), 404
    conn=_db(); c=conn.cursor()
    total=present=absent=od=0; days=set(); last_ts=None
    try:
        c.execute("SELECT s.date,e.status,e.updated_at FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id WHERE e.student_id=?", (student[0],))
        for date,st,upd in c.fetchall():
            stn=_normalize_attendance_status(st)
            if stn==UNMARKED_STATUS:
                if upd and (not last_ts or upd>last_ts): last_ts=upd
                continue
            total += 1
            if _is_attendance_credited(stn):
                present += 1; days.add(date)
                if stn=='OD': od += 1
            elif _is_attendance_absence(stn): absent += 1
            if upd and (not last_ts or upd>last_ts): last_ts=upd
    except Exception:
        try:
            c.execute('SELECT count(*) FROM attendance WHERE student_id=?',(student[0],)); total=c.fetchone()[0]
        except Exception: pass
    conn.close()
    pct = round((present/total*100),2) if total else 0.0
    return jsonify({'ok':True,'student':{'id':student[0],'name':student[1],'roll':student[2],'class':student[3],'section':student[4],'reg_no':student[5],'dob':student[7] if len(student)>7 else None},
                    'total_periods':total,'present_periods':present,'absent_periods':absent,'od_periods':od,'percentage':pct,
                    'days_present':len(days),'total_attendance_marks':total,'last_out':last_ts,
                    'note':'Manual period attendance (submitted sessions). Updates/overrides by authorized staff reflect here. No exit required; missing exit never marks absent.'})

@app.route('/notifications', methods=['GET'])
@staff_required
def get_notifications():
    conn=_db(); c=conn.cursor(); c.execute('SELECT student_id,student_name,action,timestamp FROM notifications ORDER BY id DESC LIMIT 20')
    rows=c.fetchall(); conn.close()
    role_map=_student_roles_map([r[0] for r in rows])
    data=[{'name':r[1],'student_role':role_map.get(int(r[0] or 0),''),'action':r[2],'timestamp':datetime.datetime.fromisoformat(r[3]).strftime('%Y-%m-%d %H:%M:%S') if r[3] else ''} for r in rows]
    return jsonify({'notifications':data})

@app.route('/manual_attendance', methods=['POST'])
@staff_required
def manual_attendance():
    data=request.get_json(silent=True) or {}; sid=data.get('student_id'); status=_normalize_attendance_status(data.get('status')); date_str=(data.get('date') or '').strip() or datetime.date.today().isoformat()
    if status not in ATTENDANCE_COUNTED_STATUSES: return jsonify({'ok':False,'error':'status must be a marked attendance status'}), 400
    try: sid=int(sid); datetime.date.fromisoformat(date_str)
    except Exception: return jsonify({'ok':False,'error':'Invalid student_id/date'}), 400
    ok_win, win_reason = attendance_edit_allowed(date_str)
    if not ok_win: return jsonify({'ok':False,'error':win_reason}), 403
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
    for cls in get_classes(): out[cls]=load_timetable(cls)
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
    tt=load_timetable(class_name,date_str)
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
    if code in staff or bool(re.search(r'\b'+re.escape(code)+r'\b', (cell.get('display') or '').upper())):
        return True
    if cell.get('options'):
        tt=load_timetable(class_name,date_str) or {}
        directory=[(k or '').upper() for k in (tt.get('staff_directory') or {}).keys()]
        if code in directory:
            for opt in (cell.get('options') or []):
                if re.search(r'\b'+re.escape(code)+r'\b', (opt or '').upper()): return True
    return False

def can_mark_period(class_name, date_str, period):
    role=session.get('role')
    cell=timetable_cell(class_name,date_str,period)
    disp=(cell or {}).get('display','')
    if role=='admin': return True,'Admin (universal)'
    if session.get('is_hod'): return True,'HOD override'
    if session.get('is_advisor') and session.get('advisor_class')==class_name: return True,'Class advisor (own class, full day)'
    if cell and cell.get('options'):
        if _can_mark_elective(class_name,date_str,period,cell): return True,'Mapped elective staff/advisor'
        return False,f"Not assigned: {disp} (mapped elective staff, class advisor, HOD/admin only)."
    if _is_assigned_staff(class_name,date_str,period): return True,'Assigned/handling staff'
    return False,f"Not assigned: {disp} (only listed/handling staff for this period; advisor own-class full day; HOD/admin override)."

def _student_profile_photo_path(sid):
    for ext in ('.jpg','.jpeg','.png','.webp'):
        p=os.path.join(PROFILE_PHOTOS_DIR, f'{int(sid)}{ext}')
        if os.path.exists(p): return p
    folder=os.path.join(DATASET_DIR,str(int(sid)))
    if not os.path.isdir(folder): return None
    files=[f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png','.webp'))]
    if not files: return None
    files.sort(); return os.path.join(folder,files[-1])

def _student_photo_path(sid):
    return _student_profile_photo_path(sid)

def _student_training_count(sid):
    folder=os.path.join(DATASET_DIR,str(int(sid)))
    if not os.path.isdir(folder): return 0
    return len([f for f in os.listdir(folder) if f.lower().endswith(('.jpg','.jpeg','.png','.webp'))])

@app.route('/take_attendance')
@staff_required
def take_attendance_page():
    return render_template('take_attendance.html', user=_current_user(), classes=get_classes(), today=_today_ist_str())

@app.route('/profile')
@login_required
def profile_page():
    student_row = _resolve_student_for_user() if session.get('role')=='student' else None
    student = _student_profile_dict(student_row[0]) if student_row else None
    staff=None
    if session.get('role')!='student':
        conn=_db(); c=conn.cursor()
        for col in ['phone','email','display_name','staff_code','advisor_class']: _ensure_column(c,'users',col,'TEXT')
        c.execute('SELECT username,display_name,staff_code,role,is_hod,is_advisor,advisor_class,phone,email,dob,gender,designation,department,qualification,address,date_of_joining FROM users WHERE id=?',(session.get('user_id'),))
        r=c.fetchone(); conn.close()
        if r: staff={'username':r[0],'display_name':r[1],'staff_code':r[2],'role':r[3],'is_hod':r[4],'is_advisor':r[5],'advisor_class':r[6],'phone':r[7] or '','email':r[8] or '',
                     'dob':r[9] or '','gender':r[10] or '','designation':r[11] or '','department':r[12] or '','qualification':r[13] or '','address':r[14] or '','date_of_joining':r[15] or ''}
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
        conn.commit()
        audit_log('profile', uid, 'update', None, {'password_changed':bool(newpw)}, details=f'{role} updated own profile')
        return jsonify({'ok':True})
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
    cal=_calendar_conflict(cls,date) if cls and date else []
    blocked,block_reason=_calendar_block_submit(cls,date) if cls and date else (False,None)
    return jsonify({'ok':bool(cell),'class':cls,'date':date,'period':period,'day':_day_key(date),'cell':cell,'can_mark':ok,'access_reason':reason,
                    'calendar':cal,'calendar_blocked':blocked,'calendar_reason':block_reason})

@app.route('/api/attendance/roster')
@staff_required
def api_attendance_roster():
    cls=(request.args.get('class') or '').strip(); date=request.args.get('date') or datetime.date.today().isoformat(); period=request.args.get('period')
    ok,reason=can_mark_period(cls,date,period)
    if not ok: return jsonify({'ok':False,'error':reason}), 403
    try: period=int(period)
    except Exception: return jsonify({'ok':False,'error':'Invalid period'}), 400
    edit_allowed, edit_reason = attendance_edit_allowed(date)
    cell=timetable_cell(cls,date,period) or {}
    disp=(cell.get('display') or cell.get('subject') or '').strip()
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,date,period,disp))
    sr=c.fetchone(); sess_id=sr[0] if sr else None
    existing={}
    if sess_id:
        c.execute('SELECT student_id,status,source,elective_label FROM attendance_entries WHERE session_id=?',(sess_id,))
        for r in c.fetchall(): existing[int(r[0])]={'status':_normalize_attendance_status(r[1]),'source':r[2] or '','elective_label':r[3] or ''}
    # approved OD records for this class/date/period
    approved_od=set()
    try:
        c.execute("SELECT DISTINCT student_id FROM od_records WHERE class_name=? AND date=? AND period=? AND status='OD'", (cls,date,period))
        for r in c.fetchall(): approved_od.add(int(r[0]))
    except Exception: pass
    approved_medical=set()
    try:
        c.execute("SELECT DISTINCT student_id FROM medical_records WHERE class_name=? AND date=? AND period=? AND status='Medical Leave'", (cls,date,period))
        for r in c.fetchall(): approved_medical.add(int(r[0]))
    except Exception: pass
    c.execute('SELECT id,name,roll,class,section,reg_no,dob,phone,email,guardian_name,guardian_phone,student_role FROM students WHERE upper(class)=upper(?) AND (active IS NULL OR active=1) ORDER BY roll, reg_no, name',(cls,))
    rows=c.fetchall(); conn.close()
    is_subject=_is_assigned_staff(cls,date,period)
    emaps=_elective_lookup() if cell.get('options') else {}
    slot_key=_period_slot_key(period,date)
    offering_map=((_elective_offering_map(cls).get(slot_key) or {}) if cell.get('options') else {})
    students=[]
    for r in rows:
        sid=int(r[0]); ex=existing.get(sid) or {}
        st=_normalize_attendance_status(ex.get('status'))
        od_approved = (sid in approved_od) or (st=='OD' and ex.get('source')=='od_workflow')
        if od_approved: st='OD'
        medical_approved = (sid in approved_medical) or (st=='Medical Leave' and ex.get('source')=='medical_workflow')
        if medical_approved: st='Medical Leave'
        locked_absent = (st != UNMARKED_STATUS) and (not edit_allowed)
        locked_od = od_approved and (session.get('role')!='admin') and not session.get('is_hod')
        locked_medical = medical_approved and (session.get('role')!='admin') and not session.get('is_hod')
        elective=(ex.get('elective_label') or _effective_subject(emaps,sid,cls,date,period,'')) if cell.get('options') else ''
        can_mark_student=True; student_mark_reason=edit_reason
        if cell.get('options') and session.get('role')=='staff' and not (session.get('is_hod') or session.get('role')=='admin'):
            allowed=_elective_allowed_staff_codes(offering_map,elective,cell)
            if allowed and session.get('username','').upper() not in allowed:
                can_mark_student=False; student_mark_reason='Mapped elective staff only'
        students.append({'id':r[0],'name':r[1],'roll':r[2],'class':r[3],'section':r[4],'reg_no':r[5],'dob':r[6],
                         'phone':(r[7] if len(r)>7 else '') or '','email':(r[8] if len(r)>8 else '') or '',
                         'guardian_name':(r[9] if len(r)>9 else '') or '','guardian_phone':(r[10] if len(r)>10 else '') or '',
                         'student_role':(r[11] if len(r)>11 else '') or '',
                         'photo_url':f'/student_photo_safe/{r[0]}' if _student_photo_path(r[0]) else None,
                         'selected_elective':elective,'effective_subject':elective or disp,
                          'default_status':UNMARKED_STATUS,'status':st,'unmarked':st==UNMARKED_STATUS,
                          'locked_absent':locked_absent or (not can_mark_student),'locked_od':locked_od,'od_approved':od_approved,
                          'locked_medical':locked_medical,'medical_approved':medical_approved,
                          'can_mark':can_mark_student,'mark_reason':student_mark_reason,
                          'edit_allowed':edit_allowed,'edit_reason':edit_reason,'session_id':sess_id})
    start_hour, window_days, end_hour = _attendance_window_settings()
    end_date = datetime.date.fromisoformat(date) + datetime.timedelta(days=window_days - 1)
    return jsonify({'ok':True,'class':cls,'date':date,'period':int(period),'day':_day_key(date),'cell':cell,'students':students,
                    'session_id':sess_id,'is_subject_staff':is_subject,'edit_allowed':edit_allowed,'edit_reason':edit_reason,
                    'status_options':list(ATTENDANCE_STATUSES),
                    'note':f"Unmarked periods show '-' from {start_hour:02d}:00 IST on the class date. Staff may mark until {end_hour:02d}:00 IST on day {window_days} ({end_date.isoformat()}); HOD/Admin may correct until the semester end date. Future attendance is closed. Approved OD applies date-by-date at/after 06:00 IST. Approved Medical Leave is credited and locked for staff."})

@app.route('/api/attendance/submit', methods=['POST'])
@staff_required
def api_attendance_submit():
    data=request.get_json(silent=True) or {}
    cls=(data.get('class') or '').strip(); date=(data.get('date') or '').strip(); period=data.get('period'); marks=data.get('marks') or []
    ok,reason=can_mark_period(cls,date,period)
    if not ok: return jsonify({'ok':False,'error':reason}), 403
    try: datetime.date.fromisoformat(date); period=int(period)
    except Exception: return jsonify({'ok':False,'error':'Invalid date/period'}), 400
    ok_win, win_reason = attendance_edit_allowed(date)
    if not ok_win: return jsonify({'ok':False,'error':win_reason,'edit_window':False}), 403
    blocked_cal, cal_reason = _calendar_block_submit(cls, date)
    if blocked_cal: return jsonify({'ok':False,'error':cal_reason,'calendar_blocked':True}), 403
    cell=timetable_cell(cls,date,period) or {}
    tt=load_timetable(cls,date) or {}
    version=_timetable_version_row(cls,date)
    version_id=int(version['id']) if version else None
    duration=_slot_duration_minutes(tt,period)
    disp=(cell.get('display') or (data.get('subject') or '')).strip(); subj=(cell.get('subject') or disp).strip()
    marked_by=session.get('display_name') or session.get('username') or 'staff'; now=datetime.datetime.utcnow().isoformat()
    is_admin=(session.get('role')=='admin')
    session_source='face_attendance' if is_admin and (data.get('source') or '').strip()=='face_attendance' else 'manual'
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,date,period,disp))
    row=c.fetchone()
    if row:
        sess=row[0]; c.execute('UPDATE attendance_sessions SET subject=?,subject_display=?,staff_code=?,staff_name=?,status=?,source=?,submitted_at=?,notes=?,timetable_version_id=?,duration_minutes=? WHERE id=?',
                               (subj,disp,session.get('staff_code'),marked_by,'submitted',session_source,now,(data.get('notes') or ''),version_id,duration,sess))
    else:
        c.execute('INSERT INTO attendance_sessions (class_name,date,period,subject,subject_display,staff_code,staff_name,status,source,created_at,submitted_at,notes,timetable_version_id,duration_minutes) VALUES (?,?,?,?,?,?,?, "submitted",?,?,?,?,?,?)',
                  (cls,date,period,subj,disp,session.get('staff_code'),marked_by,session_source,now,now,(data.get('notes') or ''),version_id,duration))
        sess=c.lastrowid
    saved=0; blocked=0; details=[]; absent_alerts=[]
    # approved Day OD lock (Advisor+HOD workflow) - admin/HOD can override
    approved_od=set()
    try:
        c.execute("SELECT DISTINCT student_id FROM od_records WHERE class_name=? AND date=? AND period=? AND status='OD'", (cls,date,period))
        for r in c.fetchall(): approved_od.add(int(r[0]))
    except Exception: pass
    approved_medical=set()
    try:
        c.execute("SELECT DISTINCT student_id FROM medical_records WHERE class_name=? AND date=? AND period=? AND status='Medical Leave'", (cls,date,period))
        for r in c.fetchall(): approved_medical.add(int(r[0]))
    except Exception: pass
    can_override_od = is_admin or bool(session.get('is_hod'))
    emaps=_elective_lookup() if cell.get('options') else {}
    for m in marks:
        try:
            stid=int(m.get('student_id')); status=_normalize_attendance_status(m.get('status'))
            c.execute('SELECT status,source FROM attendance_entries WHERE session_id=? AND student_id=?',(sess,stid)); prev=c.fetchone(); prev_st=_normalize_attendance_status(prev[0] if prev else None); prev_src=(prev[1] if prev else None)
            od_approved = (stid in approved_od) or (prev_st=='OD' and prev_src=='od_workflow')
            if od_approved and not can_override_od:
                if status!='OD': blocked += 1; details.append({'student_id':stid,'blocked':'Approved Day OD (Advisor+HOD) ? only HOD/Admin can change'})
                else: saved += 1
                continue
            medical_approved = (stid in approved_medical) or (prev_st=='Medical Leave' and prev_src=='medical_workflow')
            if medical_approved and not can_override_od:
                if status!='Medical Leave': blocked += 1; details.append({'student_id':stid,'blocked':'Approved Medical Leave (Advisor+HOD) ? only HOD/Admin can change'})
                else: saved += 1
                continue
            c.execute('SELECT reg_no,name FROM students WHERE id=?',(stid,)); sr=c.fetchone(); reg=sr[0] if sr else ''; sname=sr[1] if sr else (m.get('name') or '')
            face_mark = is_admin and (m.get('source') or '').strip()=='face_attendance' and status!='Medical Leave'
            src=('unmarked' if status==UNMARKED_STATUS else 'medical_workflow' if status=='Medical Leave' else ('face_attendance' if face_mark else 'manual'))
            try: conf = max(0.0, min(1.0, float(m.get('confidence')))) if face_mark and m.get('confidence') is not None else None
            except Exception: conf = None
            flagged = 1 if face_mark and m.get('flagged') else 0
            elabel=_effective_subject(emaps,stid,cls,date,period,'',version_id) if cell.get('options') else ''
            if cell.get('options') and session.get('role')=='staff' and not (session.get('is_hod') or session.get('role')=='admin'):
                allowed=_elective_allowed_staff_codes(((_elective_offering_map(cls).get(_period_slot_key(period,date)) or {})), elabel, cell)
                if allowed and session.get('username','').upper() not in allowed:
                    blocked += 1; details.append({'student_id':stid,'blocked':'Mapped elective staff only'}); continue
            c.execute("INSERT INTO attendance_entries (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at,elective_label) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET reg_no=excluded.reg_no,student_name=excluded.student_name,status=excluded.status,confidence=excluded.confidence,flagged=excluded.flagged,source=excluded.source,marked_by=excluded.marked_by,updated_at=excluded.updated_at,elective_label=COALESCE(NULLIF(excluded.elective_label,''),attendance_entries.elective_label)",
                      (sess,stid,reg,sname,status,conf,flagged,src,marked_by,now,elabel))
            if _is_attendance_absence(status) and not _is_attendance_absence(prev_st):
                try:
                    action = f"{status}: {cls} {date} P{period} {disp}".strip()
                    c.execute('INSERT INTO notifications (student_id,student_name,action,timestamp,source,marked_by) VALUES (?,?,?,?,?,?)',
                              (stid, sname or 'Student', action, now, f'{session_source}_submit', marked_by))
                    absent_alerts.append((stid, sname or 'Student', action))
                except Exception: pass
            if status=='OD':
                c.execute("SELECT 1 FROM od_records WHERE student_id=? AND class_name=? AND date=? AND period=? AND status='OD'", (stid,cls,date,period))
                if not c.fetchone():
                    c.execute('INSERT INTO od_records (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at) VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)',
                              (stid,reg,sname,cls,date,period,disp,'OD',marked_by,session.get('role') or 'staff',now))
            saved+=1
        except Exception: pass
    conn.commit(); conn.close()
    # In-app + outbox notifications are queued after the attendance transaction commits.
    seen_low = set()
    for stid, sname, action in absent_alerts:
        _enqueue_alert(stid, 'absent', 'Absent attendance', action, event_key='absent', source_ref=f'attendance:{sess}:{stid}')
        if str(get_setting('low_attendance_alerts','1') or '1') == '1' and stid not in seen_low:
            seen_low.add(stid)
            try: threshold = float(get_setting('defaulter_threshold','75') or 75)
            except Exception: threshold = 75.0
            pct = _attendance_percentage(stid)
            cooldown = int(get_setting('low_attendance_cooldown_days','7') or 7)
            if pct < threshold and not _last_alert_within(stid, 'low_attendance', cooldown):
                _enqueue_alert(stid, 'low_attendance', 'Low attendance warning',
                               f"Your attendance is {pct:.2f}%, below the required {threshold:g}%. Please meet your class advisor.",
                               event_key='low_attendance', source_ref=f'attendance:{sess}')
    audit_log('attendance_session', sess, 'submit_period', None, {'class':cls,'date':date,'period':int(period),'subject':disp,'saved':saved,'blocked':blocked}, details=(data.get('notes') or ''))
    return jsonify({'ok':True,'session_id':sess,'saved':saved,'blocked':blocked,'details':details,'class':cls,'date':date,'period':period,'subject':disp})





# --- ERP foundation: settings, audit, insights ---
ERP_DEFAULTS = {
    'semester_label': 'Current Semester',
    'semester_start': '',
    'semester_end': '',
    'defaulter_threshold': '75',
    'enforce_calendar_holidays': '0',  # 0=warn only, 1=block non-admin/HOD submit on holiday
    'attendance_start_hour': '6',
    'attendance_window_days': '15',
}
def get_setting(key, default=None):
    try:
        conn=_db(); c=conn.cursor(); c.execute('SELECT value FROM app_settings WHERE key=?',(key,)); r=c.fetchone(); conn.close()
        if r and r[0] is not None: return r[0]
    except Exception: pass
    return ERP_DEFAULTS.get(key, default)
def set_setting(key, value, actor=None):
    conn=_db(); c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
    c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
              (key, str(value) if value is not None else '', now, actor))
    conn.commit(); conn.close()
def audit_log(entity_type, entity_id, action, old_value=None, new_value=None, details=None):
    try:
        conn=_db(); c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
        c.execute('INSERT INTO attendance_audit_log(actor_role,actor_user,actor_name,entity_type,entity_id,action,old_value,new_value,details,ts) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (session.get('role'), session.get('username'), session.get('display_name'), entity_type, str(entity_id or ''), action,
                   json.dumps(old_value, default=str) if old_value is not None and not isinstance(old_value,str) else old_value,
                   json.dumps(new_value, default=str) if new_value is not None and not isinstance(new_value,str) else new_value,
                   details, now))
        conn.commit(); conn.close()
    except Exception: pass

def _now_iso(): return datetime.datetime.utcnow().isoformat()
def _today_ist_str(): return _now_ist().date().isoformat()

def _semester_info():
    sem=_current_semester()
    if sem:
        return {
            'label': sem.get('label') or get_setting('semester_label', 'Current Semester') or 'Current Semester',
            'start': sem.get('start_date') or get_setting('semester_start', '') or '',
            'end': sem.get('end_date') or get_setting('semester_end', '') or '',
            'threshold': get_setting('defaulter_threshold', '75') or '75',
            'sem_no': sem.get('sem_no'), 'batch_start_year': sem.get('batch_start_year'),
        }
    return {
        'label': get_setting('semester_label', 'Current Semester') or 'Current Semester',
        'start': get_setting('semester_start', '') or '',
        'end': get_setting('semester_end', '') or '',
        'threshold': get_setting('defaulter_threshold', '75') or '75',
    }

def _student_contacts(student_id):
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,name,reg_no,class,email,phone,guardian_phone FROM students WHERE id=?', (student_id,))
    r=c.fetchone(); conn.close()
    if not r: return None
    return {'id':r[0],'name':r[1],'reg_no':r[2],'class':r[3],'email':r[4] or '','phone':r[5] or '','guardian_phone':r[6] or ''}

def _channel_enabled(channel, event_key):
    if str(get_setting(f'notify_{channel}', '1') or '1') != '1':
        return False
    # event aliases: OD events use the shared od switch.
    key = 'od' if event_key.startswith('od_') else event_key
    return str(get_setting(f'{channel}_{key}', '1') or '1') == '1'

def _enqueue_alert(student_id, alert_type, title, body, event_key=None, source_ref=None):
    """Create an in-app alert plus optional email/SMS/push outbox rows."""
    event_key = event_key or alert_type
    student = _student_contacts(student_id)
    if not student: return None
    conn=_db(); c=conn.cursor(); now=_now_iso()
    try:
        c.execute('INSERT INTO student_alerts(student_id,alert_type,title,body,source_ref,created_at) VALUES (?,?,?,?,?,?)',
                  (student_id, alert_type, title, body, source_ref, now))
        alert_id = c.lastrowid
        for channel in ('email','sms','push'):
            if not _channel_enabled(channel, event_key): continue
            recipient = ''
            if channel == 'email': recipient = student['email']
            elif channel == 'sms': recipient = student['phone'] or student['guardian_phone']
            else: recipient = f"student:{student_id}"
            if not recipient: continue
            c.execute('INSERT INTO notification_outbox(alert_id,student_id,channel,recipient,subject,body,event_type,status,attempts,created_at) VALUES (?,?,?,?,?,?,?,?,0,?)',
                      (alert_id, student_id, channel, recipient, title, body, alert_type, 'pending', now))
        conn.commit()
        return alert_id
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        conn.close()

def _mail_configured():
    return bool(_env('SMTP_HOST') and _env('SMTP_FROM') or _env('SMTP_USER'))

def _send_email_message(to_addr, subject, body):
    host = _env('SMTP_HOST'); from_addr = _env('SMTP_FROM') or _env('SMTP_USER')
    if not host or not from_addr: raise RuntimeError('SMTP is not configured (SMTP_HOST/SMTP_FROM missing).')
    port = int(_env('SMTP_PORT', '587') or '587')
    use_ssl = _env('SMTP_SSL', '0') == '1'
    use_tls = _env('SMTP_TLS', '1') != '0'
    msg = MIMEText(body, 'plain', 'utf-8'); msg['Subject'] = subject; msg['From'] = from_addr; msg['To'] = to_addr
    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        if use_tls and not use_ssl: server.starttls(context=ssl.create_default_context())
        if _env('SMTP_USER'): server.login(_env('SMTP_USER'), _env('SMTP_PASS'))
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        try: server.quit()
        except Exception: pass

def _send_sms_message(to_number, body):
    key = _env('FAST2SMS_API_KEY')
    if not key: raise RuntimeError('SMS is not configured (FAST2SMS_API_KEY missing).')
    numbers = re.sub(r'[^0-9]', '', to_number or '')[-10:]
    if not numbers: raise RuntimeError('Student has no valid mobile number.')
    payload = {
        'route': _env('FAST2SMS_ROUTE', 'q') or 'q',
        'message': (body or '')[:900],
        'numbers': numbers,
    }
    sender = _env('FAST2SMS_SENDER_ID')
    if sender: payload['sender_id'] = sender
    req = urllib.request.Request('https://www.fast2sms.com/dev/bulkV2', data=json.dumps(payload).encode(),
                                 headers={'authorization': key, 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode('utf-8', 'ignore')
    try:
        data = json.loads(raw)
        if data.get('return') is False:
            raise RuntimeError(str(data.get('message') or raw))
    except json.JSONDecodeError:
        pass

def _send_push_message(student_id, title, body):
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        raise RuntimeError('Web Push dependency missing. Install pywebpush.')
    private_key = _env('VAPID_PRIVATE_KEY').replace('\\n', '\n')
    if not private_key or not _env('VAPID_PUBLIC_KEY'):
        raise RuntimeError('Web Push is not configured (VAPID keys missing).')
    conn=_db(); c=conn.cursor()
    c.execute('SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE student_id=? AND active=1', (student_id,))
    rows=c.fetchall(); conn.close()
    if not rows:
        return 'skipped: no push subscription'
    claims = {'sub': _env('VAPID_SUBJECT', 'mailto:admin@example.com')}
    payload = json.dumps({'title': title, 'body': body, 'url': '/student_dashboard'})
    sent = 0; errors = []
    for sid, endpoint, p256dh, auth in rows:
        try:
            webpush(subscription_info={'endpoint':endpoint,'keys':{'p256dh':p256dh,'auth':auth}},
                    data=payload, vapid_private_key=private_key, vapid_claims=claims, timeout=20)
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                conn=_db(); conn.execute('UPDATE push_subscriptions SET active=0 WHERE id=?', (sid,)); conn.commit(); conn.close()
            errors.append(str(e))
        except Exception as e:
            errors.append(str(e))
    if sent == 0 and errors:
        raise RuntimeError('; '.join(errors)[:800])
    return f'sent {sent}'

def _send_outbox_item(row):
    channel, recipient, subject, body = row['channel'], row['recipient'], row['subject'], row['body']
    if channel == 'email': _send_email_message(recipient, subject, body)
    elif channel == 'sms': _send_sms_message(recipient, f"{subject}\n{body}")
    elif channel == 'push': return _send_push_message(row['student_id'], subject, body)
    else: raise RuntimeError(f'Unknown channel: {channel}')
    return 'sent'

def _reclaim_stale_outbox(conn, stale_minutes=10):
    """Release rows claimed by a worker that died before finishing."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=int(stale_minutes))).isoformat()
    conn.execute("""UPDATE notification_outbox
                    SET status='failed', claimed_at=NULL,
                        last_error=COALESCE(last_error,'stale claim recovered')
                    WHERE status='processing' AND (claimed_at IS NULL OR claimed_at < ?)""", (cutoff,))
    conn.commit()

def process_notification_outbox(limit=25, only_ids=None):
    conn=_db(); conn.row_factory=sqlite3.Row
    _reclaim_stale_outbox(conn)
    c=conn.cursor()
    if only_ids:
        ids=[int(i) for i in only_ids]
        if not ids:
            conn.close(); return {'processed':0,'sent':0,'failed':0,'skipped':0}
        marks=','.join('?' for _ in ids)
        c.execute(f"SELECT * FROM notification_outbox WHERE id IN ({marks}) AND status IN ('pending','failed') AND attempts < 5 ORDER BY id", tuple(ids))
    else:
        c.execute("SELECT * FROM notification_outbox WHERE status IN ('pending','failed') AND attempts < 5 ORDER BY id LIMIT ?", (int(limit),))
    rows=c.fetchall(); conn.close()
    result = {'processed':0,'sent':0,'failed':0,'skipped':0}
    for row in rows:
        now=_now_iso()
        # Atomically claim the row so concurrent workers cannot send it twice.
        # Compare-and-swap on attempts: if another worker already touched this
        # row after our SELECT, its attempts value changed and we skip it.
        conn=_db(); c=conn.cursor()
        c.execute("""UPDATE notification_outbox SET status='processing', attempts=attempts+1, claimed_at=?
                     WHERE id=? AND status IN ('pending','failed') AND attempts=? AND attempts < 5""",
                  (now, row['id'], row['attempts']))
        claimed = c.rowcount == 1
        conn.commit(); conn.close()
        if not claimed:
            continue
        try:
            msg = _send_outbox_item(row)
            status = 'skipped' if str(msg).startswith('skipped') else 'sent'
            conn=_db(); c=conn.cursor()
            c.execute('UPDATE notification_outbox SET status=?, last_error=?, sent_at=?, claimed_at=NULL WHERE id=?',
                      (status, (msg if status=='skipped' else None), now if status=='sent' else None, row['id']))
            conn.commit(); conn.close()
            result[status] = result.get(status,0) + 1
        except Exception as e:
            conn=_db(); c=conn.cursor()
            c.execute('UPDATE notification_outbox SET status=?, last_error=?, claimed_at=NULL WHERE id=?', ('failed', str(e)[:900], row['id']))
            conn.commit(); conn.close()
            result['failed'] += 1
        result['processed'] += 1
    return result

def _notify_worker_loop():
    while True:
        try:
            if any(v.get('configured') for v in _notification_config_status().values()):
                process_notification_outbox(25)
        except Exception:
            pass
        time.sleep(30)

def _start_notify_worker():
    if (os.environ.get('ERP_NOTIFY_WORKER') or '1') == '0':
        return
    if getattr(_start_notify_worker,'_started',False):
        return
    # Flask debug reloader imports the app in both parent and child; only start in the child.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' and (os.environ.get('FLASK_DEBUG') == '1' or app.debug):
        return
    _start_notify_worker._started=True
    threading.Thread(target=_notify_worker_loop, name='notification-worker', daemon=True).start()

def _notification_config_status():
    return {
        'email': {'configured': bool(_env('SMTP_HOST') and (_env('SMTP_FROM') or _env('SMTP_USER'))), 'detail': _env('SMTP_HOST') or 'SMTP_HOST not set'},
        'sms': {'configured': bool(_env('FAST2SMS_API_KEY')), 'detail': 'Fast2SMS key ' + ('set' if _env('FAST2SMS_API_KEY') else 'not set')},
        'push': {'configured': bool(_env('VAPID_PUBLIC_KEY') and _env('VAPID_PRIVATE_KEY')), 'detail': 'VAPID keys ' + ('set' if _env('VAPID_PUBLIC_KEY') and _env('VAPID_PRIVATE_KEY') else 'not set')},
    }

def _last_alert_within(student_id, alert_type, days=7):
    try:
        cutoff=(datetime.datetime.utcnow()-datetime.timedelta(days=int(days))).isoformat()
        conn=_db(); c=conn.cursor()
        c.execute('SELECT id FROM student_alerts WHERE student_id=? AND alert_type=? AND created_at>=? LIMIT 1', (student_id, alert_type, cutoff))
        r=c.fetchone(); conn.close(); return bool(r)
    except Exception: return False

def _attendance_percentage(student_id):
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT SUM(CASE WHEN LOWER(status) IN """+ATTENDANCE_COUNTED_SQL+""" THEN 1 ELSE 0 END),
                        SUM(CASE WHEN LOWER(status) IN """+ATTENDANCE_CREDITED_SQL+""" THEN 1 ELSE 0 END)
                 FROM attendance_entries WHERE student_id=?""", (student_id,))
    total, present = c.fetchone(); conn.close()
    total = int(total or 0); present = int(present or 0)
    return round((present * 100.0 / total), 2) if total else 0.0

def _calendar_conflict(class_name, date_str):
    # Returns holiday/override info affecting attendance (if any). Enforcement controlled by setting.
    try:
        items = _calendar_items(class_name, date_str, date_str)
        hits=[]
        for it in items:
            if int(it.get('affects_attendance') or 0) != 1: continue
            t=(it.get('type') or '').lower()
            if t in ('holiday','exam','event','working_day_override','note'):
                hits.append(it)
        return hits
    except Exception:
        return []

def _calendar_block_submit(class_name, date_str):
    # Optional enforcement: block staff submit on holiday if setting enabled. Admin/HOD override always.
    if _is_admin_or_hod(): return False, None
    if str(get_setting('enforce_calendar_holidays','0') or '0') != '1': return False, None
    hits=_calendar_conflict(class_name, date_str)
    hol=[h for h in hits if (h.get('type') or '').lower()=='holiday']
    if hol:
        return True, 'Attendance submission blocked: ' + (hol[0].get('title') or 'Holiday') + ' (' + date_str + ') per academic calendar. Admin/HOD may override via setting/policy.'
    return False, None

# --- Time (IST) and attendance edit window (Phase A) ---
IST_TZ = ZoneInfo('Asia/Kolkata') if ZoneInfo else None
ATTENDANCE_CUTOFF_HOUR = 22
OD_APPLY_HOUR = 6
ABSENCE_WORKER_INTERVAL = 60

def _setting_int(key, default, low, high):
    try: return max(low, min(high, int(float(get_setting(key, default)))))
    except Exception: return default

def _attendance_window_settings():
    return (_setting_int('attendance_start_hour', 6, 0, 23),
            _setting_int('attendance_window_days', 15, 1, 60),
            _setting_int('attendance_cutoff_hour', ATTENDANCE_CUTOFF_HOUR, 0, 23))

def _now_ist():
    # IST = UTC+05:30 (fallback if zoneinfo unavailable)
    utc = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    if IST_TZ:
        return utc.astimezone(IST_TZ)
    return utc + datetime.timedelta(hours=5, minutes=30)

def _is_admin_or_hod():
    return session.get('role') == 'admin' or bool(session.get('is_hod'))

@app.context_processor
def inject_erp_context():
    sem = _semester_info()
    return {
        'erp_classes': get_classes(),
        'erp_semester': sem,
        'erp_today': _today_ist_str(),
        'erp_brand': 'Attendance ERP',
        'erp_settings': {
            'semester_label': sem['label'],
            'defaulter_threshold': sem['threshold'],
            'enforce_calendar_holidays': get_setting('enforce_calendar_holidays', '0'),
        },
    }

def _attendance_window_status(att_d, now=None, is_override=False):
    now = now or _now_ist()
    today = now.date()
    if att_d > today:
        return False, 'Future attendance cannot be marked or changed.'
    _sem, sem_end = _semester_end_for_date(att_d.isoformat())
    if sem_end and att_d > sem_end:
        return False, f'Semester ended on {sem_end.isoformat()}; attendance changes are closed.'
    if is_override:
        until = f' until semester end {sem_end.isoformat()}' if sem_end else ''
        return True, f'Admin/HOD override{until}'
    start_hour, window_days, end_hour = _attendance_window_settings()
    tz = now.tzinfo
    start = datetime.datetime.combine(att_d, datetime.time(start_hour, 0, 0)).replace(tzinfo=tz)
    end_date = att_d + datetime.timedelta(days=window_days - 1)
    end = datetime.datetime.combine(end_date, datetime.time(end_hour, 0, 0)).replace(tzinfo=tz)
    if now < start:
        return False, f'Staff edit window opens at {start_hour:02d}:00 IST on {att_d.isoformat()}.'
    if now > end:
        return False, f'Staff edit window closed at {end_hour:02d}:00 IST on {end_date.isoformat()}. HOD/Admin may correct until the semester end date.'
    return True, f'Allowed (until {end_hour:02d}:00 IST on {end_date.isoformat()})'

def attendance_edit_allowed(attendance_date_str):
    try:
        att_d = datetime.date.fromisoformat((attendance_date_str or '').strip())
    except Exception:
        return False, 'Invalid attendance date.'
    return _attendance_window_status(att_d, is_override=_is_admin_or_hod())

# --- Academic calendar (admin) and student dashboard APIs ---
def _parse_iso_date(d):
    try: return datetime.date.fromisoformat((d or '').strip())
    except Exception: return None

def _calendar_items(class_name=None, date_from=None, date_to=None):
    conn=_db(); c=conn.cursor()
    q='SELECT id,title,date,end_date,type,affects_attendance,class_name,notes,created_by,created_at FROM academic_calendar WHERE 1=1'
    params=[]
    if class_name:
        q += ' AND (class_name=? OR class_name IS NULL OR class_name="")'; params.append(class_name)
    if date_from: q += ' AND COALESCE(end_date,date)>=?'; params.append(date_from)
    if date_to: q += ' AND date<=?'; params.append(date_to)
    q += ' ORDER BY date DESC, id DESC'
    try:
        c.execute(q, params); rows=c.fetchall()
    except Exception:
        rows=[]
    conn.close()
    keys=['id','title','date','end_date','type','affects_attendance','class_name','notes','created_by','created_at']
    return [dict(zip(keys,r)) for r in rows]

def _calendar_map_for_range(class_name, d1, d2):
    items=_calendar_items(class_name, d1.isoformat(), d2.isoformat())
    by_date={}
    for it in items:
        a=_parse_iso_date(it.get('date')); b=_parse_iso_date(it.get('end_date')) or a
        if not a: continue
        if b < a: b=a
        cur=max(a,d1); end=min(b,d2)
        while cur<=end:
            by_date.setdefault(cur.isoformat(), []).append(it)
            cur += datetime.timedelta(days=1)
    return by_date

# --- Semesters (admin-managed; legacy global + batch-relative sem 1-8) ---
SEMESTER_COLS=['id','label','start_date','end_date','is_current','created_by','created_at','updated_at','batch_start_year','sem_no','is_auto']

def _semester_row_dict(r):
    d=dict(zip(SEMESTER_COLS,r))
    d['is_current']=int(d.get('is_current') or 0)
    for k in ('batch_start_year','sem_no','is_auto'):
        try: d[k]=int(d[k]) if d.get(k) not in (None,'') else None
        except Exception: d[k]=None
    return d

def _semester_rows(batch_start_year=None, include_legacy=True):
    conn=_db(); c=conn.cursor(); rows=[]
    try:
        q="SELECT id,label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto FROM semesters WHERE 1=1"
        params=[]
        if batch_start_year is not None:
            q+=" AND batch_start_year=?"; params.append(int(batch_start_year))
        elif not include_legacy:
            q+=" AND batch_start_year IS NOT NULL"
        q+=" ORDER BY CASE WHEN batch_start_year IS NULL THEN 1 ELSE 0 END, batch_start_year DESC, COALESCE(sem_no,99) ASC, is_current DESC, COALESCE(start_date,'') DESC, id DESC"
        c.execute(q,params); rows=c.fetchall()
    except Exception:
        rows=[]
    finally:
        conn.close()
    return [_semester_row_dict(r) for r in rows]

def _semester_by_id(sid):
    try: sid=int(sid)
    except Exception: return None
    conn=_db(); c=conn.cursor()
    try:
        c.execute("SELECT id,label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto FROM semesters WHERE id=?",(sid,))
        r=c.fetchone(); return _semester_row_dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()

def _batch_semester_row(batch_start_year, sem_no):
    try: by=int(batch_start_year); sn=int(sem_no)
    except Exception: return None
    conn=_db(); c=conn.cursor()
    try:
        c.execute("SELECT id,label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto FROM semesters WHERE batch_start_year=? AND sem_no=?",(by,sn))
        r=c.fetchone(); return _semester_row_dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()

def get_or_create_batch_semester(batch_start_year, sem_no, actor='system'):
    row=_batch_semester_row(batch_start_year, sem_no)
    if row: return row
    by=int(batch_start_year); sn=int(sem_no)
    start,end=default_batch_sem_dates(by,sn)
    now=_now_iso(); who=_norm(actor) or 'system'; label=batch_sem_label(by,sn)
    conn=_db(); c=conn.cursor()
    try:
        c.execute("""INSERT INTO semesters(label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto)
                     VALUES (?,?,?,0,?,?,?,?,?,1)""",(label,start,end,who,now,now,by,sn))
        conn.commit(); sid=c.lastrowid
    except Exception:
        conn.rollback(); sid=None
    finally:
        conn.close()
    return _semester_by_id(sid) if sid else _batch_semester_row(by,sn)

def ensure_batch_semesters(batch_start_year, actor='system'):
    by=int(batch_start_year)
    return [get_or_create_batch_semester(by, sn, actor=actor) for sn in range(1,9)]

def _normalize_sem_no(v):
    try:
        n=int(v)
        return n if 1<=n<=8 else None
    except Exception:
        return None

def current_sem_for_batch(batch_start_year, on_date=None):
    by=int(batch_start_year)
    today=_parse_iso_date(on_date) if on_date else _now_ist().date()
    rows=_semester_rows(batch_start_year=by, include_legacy=False)
    for s in rows:
        a=_parse_iso_date(s.get('start_date')); b=_parse_iso_date(s.get('end_date'))
        if a and b and a<=today<=b:
            return int(s.get('sem_no') or 0) or None
    for sn in range(1,9):
        a,b=[_parse_iso_date(x) for x in default_batch_sem_dates(by,sn)]
        if a and b and a<=today<=b: return sn
    # before first sem start -> 1, after last sem end -> 8
    a1,_=default_batch_sem_dates(by,1); _,b8=default_batch_sem_dates(by,8)
    if today < _parse_iso_date(a1): return 1
    if today > _parse_iso_date(b8): return 8
    return 1

def visible_sem_nos(role, batch_start_year, on_date=None):
    cur=current_sem_for_batch(batch_start_year,on_date=on_date) or 1
    role=_norm(role).lower()
    if role in ('admin','hod'):
        return list(range(1,9)), cur
    return list(range(1,cur+1)), cur

def _current_semester():
    # Legacy/global current semester (compat). Batch-relative current is resolved per batch.
    rows=_semester_rows()
    for s in rows:
        if s.get('is_current') and s.get('batch_start_year') is None: return s
    for s in rows:
        if s.get('is_current'): return s
    return rows[0] if rows else None

def _semester_end_for_date(date_str, class_name=None, batch_start_year=None):
    """Find semester containing date_str; prefer batch-relative when batch/class known."""
    d=_parse_iso_date(date_str)
    info=parse_cohort_class(class_name) if class_name else None
    by=batch_start_year or (info.get('batch_start_year') if info else None)
    if d and by is not None:
        try:
            by=int(by)
            rows=_semester_rows(batch_start_year=by, include_legacy=False)
            for s in rows:
                a=_parse_iso_date(s.get('start_date')); b=_parse_iso_date(s.get('end_date'))
                if a and b and a<=d<=b: return s,b
            # fallback by default date map
            sn=current_sem_for_batch(by, on_date=d.isoformat())
            s=get_or_create_batch_semester(by,sn)
            return s, _parse_iso_date((s or {}).get('end_date'))
        except Exception:
            pass
    if d:
        for s in _semester_rows():
            a=_parse_iso_date(s.get('start_date')); b=_parse_iso_date(s.get('end_date'))
            if a and b and a<=d<=b: return s,b
    s=_current_semester()
    end=_parse_iso_date((s or {}).get('end_date') or get_setting('semester_end',''))
    return s,end

def _last_working_day(class_name, start, end):
    """Last timetable working day <= end (holidays skipped, overrides honored)."""
    if not end: return end
    if not start or start>end: start=end
    cur=end
    for _ in range(400):
        if cur<start: break
        dk=_day_key(cur.isoformat())
        tt=load_timetable(class_name,cur.isoformat()) if class_name else None
        sched=(tt or {}).get('schedule') or {}
        has_sched=bool(sched.get(dk)) if tt else (dk in ('MON','TUE','WED','THU','FRI','SAT'))
        if has_sched:
            cal=_calendar_map_for_range(class_name, cur, cur).get(cur.isoformat(),[])
            override=any((str(it.get('type') or '').lower()=='working_day_override') for it in cal)
            holiday=any((str(it.get('type') or '').lower()=='holiday' and int(it.get('affects_attendance') or 0)==1) for it in cal)
            if override or not holiday: return cur
        cur -= datetime.timedelta(days=1)
    return end

def _semester_window(semester, class_name=None, today=None):
    """Return (from_date, to_date). Current = start..today; completed = start..last working day."""
    today=today or _now_ist().date()
    start=_parse_iso_date((semester or {}).get('start_date'))
    end=_parse_iso_date((semester or {}).get('end_date'))
    is_current=bool((semester or {}).get('is_current'))
    if is_current:
        d2=today
        if end and end<d2: d2=end
    elif end:
        d2=_last_working_day(class_name, start or end, end)
    else:
        d2=today
    if not start:
        try:
            conn=_db(); c=conn.cursor()
            c.execute("SELECT MIN(date) FROM attendance_sessions WHERE date>=date('now','-180 days')")
            row=c.fetchone(); conn.close()
            start=_parse_iso_date(row[0] if row else None)
        except Exception:
            start=None
    d1=start or d2
    if d1>d2: d1=d2
    if (d2-d1).days>366: d1=d2-datetime.timedelta(days=366)
    return d1,d2

def _semester_json(s, batch_start_year=None, on_date=None):
    if not s: return None
    by=s.get('batch_start_year') if s.get('batch_start_year') is not None else batch_start_year
    sn=s.get('sem_no')
    cur=None
    if by is not None:
        try: cur=current_sem_for_batch(int(by), on_date=on_date)
        except Exception: cur=None
    status=None
    if by is not None and sn is not None and cur is not None:
        sn_i=int(sn)
        status='current' if sn_i==int(cur) else ('upcoming' if sn_i>int(cur) else 'completed')
    elif int(s.get('is_current') or 0)==1:
        status='current'
    return {'id':s.get('id'),'label':s.get('label'),'start_date':s.get('start_date') or '',
            'end_date':s.get('end_date') or '','is_current':int(s.get('is_current') or 0),
            'batch_start_year':by,'sem_no':sn,'is_auto':int(s.get('is_auto') or 0) if s.get('is_auto') is not None else None,
            'batch_label':batch_label(by) if by is not None else '','status':status}

def _semester_display_window(s, class_name=None):
    d1,d2=_semester_window(s, class_name)
    return d1.isoformat(), d2.isoformat()

def _sync_current_semester_settings():
    s=_current_semester()
    if not s: return
    who=session.get('display_name') or session.get('username') or 'system'
    set_setting('semester_label', s.get('label') or '', who)
    set_setting('semester_start', s.get('start_date') or '', who)
    set_setting('semester_end', s.get('end_date') or '', who)

def _slot_end_time(tt, period):
    for s in (tt or {}).get('slots') or []:
        try:
            if int(s.get('period') or 0)==int(period):
                end=(s.get('end') or '').strip()
                if not end: return None
                return datetime.datetime.strptime(end[:5], '%H:%M').time()
        except Exception:
            continue
    return None

def _slot_duration_minutes(tt, period):
    for s in (tt or {}).get('slots') or []:
        try:
            if int(s.get('period') or 0)!=int(period): continue
            start=(s.get('start') or '').strip(); end=(s.get('end') or '').strip()
            if not start or not end: return None
            t1=datetime.datetime.strptime(start[:5],'%H:%M'); t2=datetime.datetime.strptime(end[:5],'%H:%M')
            mins=int((t2-t1).total_seconds()//60)
            return mins if mins>0 else None
        except Exception:
            continue
    return None

def _period_ended(date_str, period, tt, now_ist):
    d=_parse_iso_date(date_str)
    if not d: return False
    today=now_ist.date()
    if d<today: return True
    if d>today: return False
    end=_slot_end_time(tt, period)
    return bool(end and now_ist.time()>=end)

def _unmarked_placeholder_due(date_str, now_ist):
    d=_parse_iso_date(date_str)
    if not d: return False
    today=now_ist.date()
    if d<today: return True
    if d>today: return False
    start_hour, _days, _end_hour = _attendance_window_settings()
    return now_ist.hour >= start_hour

def _active_students_for_class(conn, class_name):
    c=conn.cursor()
    c.execute('SELECT id,reg_no,name FROM students WHERE upper(class)=upper(?) AND (active IS NULL OR active=1) ORDER BY roll, reg_no, name',(class_name,))
    return c.fetchall()

def _materialize_default_absent(class_name=None, date_from=None, date_to=None, conn=None, semester=None):
    """Persist unmarked '-' rows for timetable periods with no explicit entry yet.
    Idempotent: existing Present/Late/OD/N/manual marks are never overwritten."""
    own = conn is None
    if own: conn = _db()
    now = _now_ist(); today = now.date(); now_iso = datetime.datetime.utcnow().isoformat()
    classes = [class_name] if class_name else get_classes()
    stats = {'classes':0,'sessions_created':0,'entries_created':0}
    try:
        for cls in classes:
            base_tt = load_timetable(cls,today.isoformat())
            if not base_tt: continue
            if not (base_tt.get('schedule') or {}): continue
            students = _active_students_for_class(conn, cls)
            if not students: continue
            if date_from or date_to:
                d1 = _parse_iso_date(date_from) or today
                d2 = _parse_iso_date(date_to) or today
            else:
                d1, d2 = _semester_window(semester or _current_semester(), cls, today)
            if not d1 or not d2: continue
            if d2 > today: d2 = today
            if d1 > d2: continue
            cal_map = _calendar_map_for_range(cls, d1, d2)
            emaps = _elective_lookup()
            c = conn.cursor()
            cur = d1
            while cur <= d2:
                ds = cur.isoformat()
                day_cal = cal_map.get(ds, []) or []
                holiday = any((str(it.get('type') or '').lower()=='holiday' and int(it.get('affects_attendance') or 0)==1) for it in day_cal)
                override = any((str(it.get('type') or '').lower()=='working_day_override') for it in day_cal)
                if holiday and not override:
                    cur += datetime.timedelta(days=1); continue
                dk = _day_key(ds)
                day_tt = load_timetable(cls,ds) or base_tt
                day_version = _timetable_version_row(cls,ds)
                day_version_id = int(day_version['id']) if day_version else None
                for cell in (day_tt.get('schedule') or {}).get(dk) or []:
                    try: per = int(cell.get('period') or 0)
                    except Exception: continue
                    if per < 1 or per > 7: continue
                    if not _unmarked_placeholder_due(ds, now): continue
                    duration = _slot_duration_minutes(day_tt, per)
                    disp = (cell.get('display') or cell.get('subject') or f'P{per}').strip()
                    subj = (cell.get('subject') or disp).strip()
                    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(cls,ds,per,disp))
                    sr = c.fetchone()
                    if sr:
                        sess = sr[0]
                        c.execute('UPDATE attendance_sessions SET timetable_version_id=COALESCE(timetable_version_id,?), duration_minutes=COALESCE(duration_minutes,?) WHERE id=?',(day_version_id,duration,sess))
                    else:
                        c.execute('INSERT INTO attendance_sessions (class_name,date,period,subject,subject_display,staff_code,staff_name,status,source,created_at,submitted_at,notes,timetable_version_id,duration_minutes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (cls,ds,per,subj,disp,'SYSTEM','System (unmarked)','submitted','auto_absent',now_iso,now_iso,'Auto-created for unmarked period',day_version_id,duration))
                        sess = c.lastrowid; stats['sessions_created'] += 1
                    before = conn.total_changes
                    c.executemany('INSERT INTO attendance_entries (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at,elective_label) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,student_id) DO NOTHING',
                                  [(sess,sid,reg or '',name or '',UNMARKED_STATUS,None,0,'auto_absent','system',now_iso,
                                    _effective_subject(emaps,sid,cls,ds,per,'',day_version_id) if cell.get('options') else '') for sid,reg,name in students])
                    stats['entries_created'] += conn.total_changes - before
                if own: conn.commit()
                cur += datetime.timedelta(days=1)
            stats['classes'] += 1
    except Exception:
        if own: conn.rollback()
        raise
    finally:
        if own: conn.close()
    return stats

def _timetable_cell_for_slot(tt, date_str, period, display=None):
    cells=((tt or {}).get('schedule') or {}).get(_day_key(date_str)) or []
    wanted=int(period or 0); wanted_display=_norm(display)
    fallback=None
    for cell in cells:
        try: per=int(cell.get('period') or 0)
        except Exception: continue
        if per!=wanted: continue
        fallback=fallback or cell
        cell_display=_norm(cell.get('display') or cell.get('subject') or '')
        if not wanted_display or cell_display==wanted_display: return cell
    return fallback

def _slot_time_text(tt, period):
    for s in (tt or {}).get('slots') or []:
        try:
            if int(s.get('period') or 0)!=int(period or 0): continue
            start=(s.get('start') or '').strip()[:5]; end=(s.get('end') or '').strip()[:5]
            if start or end: return f'{start or "?"}-{end or "?"}'
        except Exception:
            continue
    return ''

def _staff_alert_recipients(conn, tt, cls, cell):
    c=conn.cursor()
    directory={_upper(k):_norm(v) for k,v in ((tt or {}).get('staff_directory') or {}).items()}
    recipients=[]; seen=set()
    def add(code, name=''):
        code=_upper(code)
        if not code or code in seen: return
        seen.add(code)
        display=_norm(name) or directory.get(code) or code
        c.execute('SELECT id,display_name FROM users WHERE upper(COALESCE(staff_code,username))=upper(?) AND COALESCE(active,1)=1 ORDER BY id LIMIT 1',(code,))
        u=c.fetchone()
        if u:
            display=_norm(u[1]) or display
        recipients.append({'code':code,'name':display,'user_id':int(u[0]) if u else None})
    for code in ((cell or {}).get('staff') or []):
        add(code, directory.get(_upper(code), ''))
    if not recipients:
        adv=(tt or {}).get('class_advisor') or {}
        add(adv.get('initials'), adv.get('name'))
    if not recipients:
        c.execute("""SELECT staff_code,display_name FROM users
                     WHERE upper(COALESCE(advisor_class,''))=upper(?) AND COALESCE(active,1)=1
                       AND COALESCE(staff_code,username) IS NOT NULL
                     ORDER BY id LIMIT 1""",(cls,))
        u=c.fetchone()
        if u: add(u[0], u[1])
    return recipients or [{'code':'UNASSIGNED','name':'Unassigned (admin visibility)','user_id':None}]

def _generate_unmarked_staff_alerts(conn=None):
    """Create daily staff digests for still-unmarked past periods while staff can mark them."""
    own = conn is None
    if own: conn=_db()
    c=conn.cursor(); now=_now_ist(); today=now.date().isoformat()
    stats={'digests':0,'inserted':0,'updated':0,'removed':0}
    try:
        c.execute("""SELECT s.id,s.class_name,s.date,s.period,s.subject_display,COUNT(e.id)
                     FROM attendance_sessions s JOIN attendance_entries e ON e.session_id=s.id
                     WHERE LOWER(COALESCE(e.status,''))=? AND s.date<?
                     GROUP BY s.id ORDER BY s.date DESC,s.class_name,s.period""",(UNMARKED_STATUS,today))
        pending=c.fetchall(); digests={}
        for sess,cls,ds,period,subject,count in pending:
            att_d=_parse_iso_date(ds)
            if not att_d: continue
            allowed,_reason=_attendance_window_status(att_d, now=now)
            if not allowed: continue
            tt=load_timetable(cls,ds) or {}
            cell=_timetable_cell_for_slot(tt,ds,period,subject)
            slot=_slot_time_text(tt,period)
            label=f'{ds} | P{period} | {cls} | {_norm(subject) or "Period " + str(period)}'
            if slot: label += f' | {slot}'
            label += f' | {int(count or 0)} unmarked student(s)'
            for rec in _staff_alert_recipients(conn,tt,cls,cell):
                key=(rec['code'],ds)
                d=digests.setdefault(key,{'recipient':rec,'date':ds,'lines':[]})
                if label not in d['lines']: d['lines'].append(label)
        active_refs=set()
        for (code,ds),d in sorted(digests.items(), key=lambda kv:(kv[0][0],kv[0][1])):
            rec=d['recipient']; source_ref=f'unmarked_digest:{code}:{ds}'
            active_refs.add(source_ref)
            title=('Unassigned unmarked attendance' if code=='UNASSIGNED' else 'Unmarked attendance reminder')+f' - {ds}'
            body='\n'.join(d['lines'])
            existing=c.execute('SELECT id,body FROM staff_alerts WHERE source_ref=? ORDER BY id DESC LIMIT 1',(source_ref,)).fetchone()
            if existing:
                if (existing[1] or '')!=body:
                    c.execute('UPDATE staff_alerts SET title=?,body=?,read_at=NULL WHERE id=?',(title,body,existing[0]))
                    stats['updated']+=1
            else:
                c.execute("""INSERT INTO staff_alerts(recipient_code,recipient_user_id,title,body,source_ref,created_at)
                             VALUES (?,?,?,?,?,?)""",(code,rec.get('user_id'),title,body,source_ref,_now_iso()))
                stats['inserted']+=1
            stats['digests']+=1
        rows=c.execute("SELECT id,source_ref FROM staff_alerts WHERE source_ref LIKE 'unmarked_digest:%'").fetchall()
        stale=[int(r[0]) for r in rows if r[1] not in active_refs]
        for alert_id in stale:
            c.execute('DELETE FROM staff_alerts WHERE id=?',(alert_id,))
        stats['removed']=len(stale)
        if own: conn.commit()
    except Exception:
        if own: conn.rollback()
        raise
    finally:
        if own: conn.close()
    return stats

def _absence_worker_loop():
    backfill_done = False
    while True:
        try:
            if not backfill_done:
                _materialize_default_absent()
                backfill_done = True
            else:
                today = _now_ist().date().isoformat()
                _materialize_default_absent(date_from=today, date_to=today)
            _generate_unmarked_staff_alerts()
            _apply_due_approved_od()
            _apply_due_approved_medical()
        except Exception:
            pass
        time.sleep(ABSENCE_WORKER_INTERVAL)

def _start_absence_worker():
    if (os.environ.get('ERP_ABSENCE_WORKER') or '1') == '0':
        return
    if getattr(_start_absence_worker,'_started',False):
        return
    # When run via `python app.py` the Werkzeug reloader imports once in the parent and once in the child.
    if __name__ == '__main__' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    _start_absence_worker._started = True
    threading.Thread(target=_absence_worker_loop, name='absence-worker', daemon=True).start()

@app.route('/admin/semesters')
@admin_required
def admin_semesters_page():
    batches=[{'batch_start_year':by,'label':batch_label(by)} for by in BATCH_START_YEARS]
    return render_template('admin_semesters.html', user=_current_user(), batches=batches)

@app.route('/api/semesters')
@login_required
def api_semesters():
    batch_raw=request.args.get('batch_start_year')
    class_name=request.args.get('class') or request.args.get('class_name')
    info=parse_cohort_class(class_name) if class_name else None
    by=None
    if batch_raw not in (None,''):
        try: by=int(batch_raw)
        except Exception: by=None
    if by is None and info: by=info.get('batch_start_year')
    role=session.get('role') or 'staff'
    today=_today_ist_str()
    if by is not None:
        ensure_batch_semesters(by, actor=session.get('username') or 'system')
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        visible, cur_no = visible_sem_nos(role, by, on_date=today)
        rows=[_semester_json(s,batch_start_year=by,on_date=today) for s in sem_rows if int(s.get('sem_no') or 0) in set(visible)]
        cur_id=next((r['id'] for r in rows if int(r.get('sem_no') or 0)==int(cur_no)), (rows[0]['id'] if rows else None))
        return jsonify({'ok':True,'batch_start_year':by,'batch_label':batch_label(by),'current_sem_no':cur_no,'current_id':cur_id,'semesters':rows})
    rows=[_semester_json(s,on_date=today) for s in _semester_rows()]
    cur=next((s['id'] for s in rows if s.get('is_current') or s.get('status')=='current'), (rows[0]['id'] if rows else None))
    return jsonify({'ok':True,'semesters':rows,'current_id':cur})

@app.route('/api/admin/semesters', methods=['GET'])
@admin_required
def api_admin_semesters_list():
    batch_raw=request.args.get('batch_start_year')
    by=None
    if batch_raw not in (None,''):
        try: by=int(batch_raw)
        except Exception: by=None
    today=_today_ist_str()
    if by is not None:
        ensure_batch_semesters(by, actor=session.get('username') or 'admin')
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        _,cur_no=visible_sem_nos('admin',by,on_date=today)
        rows=[_semester_json(s,batch_start_year=by,on_date=today) for s in sem_rows]
        cur_id=next((r['id'] for r in rows if int(r.get('sem_no') or 0)==int(cur_no)), None)
        return jsonify({'ok':True,'batch_start_year':by,'batch_label':batch_label(by),'current_sem_no':cur_no,'current_id':cur_id,'semesters':rows,'batches':BATCH_REGISTRY})
    rows=[_semester_json(s,on_date=today) for s in _semester_rows()]
    return jsonify({'ok':True,'semesters':rows,'current_id':next((s['id'] for s in rows if s.get('is_current') or s.get('status')=='current'), None),'batches':BATCH_REGISTRY})

@app.route('/api/admin/semesters', methods=['POST'])
@admin_required
def api_admin_semesters_save():
    data=request.get_json(silent=True) or request.form.to_dict()
    who=session.get('display_name') or session.get('username') or 'admin'
    now=datetime.datetime.utcnow().isoformat()
    # batch-sem save
    if data.get('batch_start_year') not in (None,'') and data.get('sem_no') not in (None,''):
        try:
            by=int(data.get('batch_start_year')); sn=int(data.get('sem_no'))
        except Exception:
            return jsonify({'ok':False,'error':'Invalid batch/sem'}),400
        if sn<1 or sn>8: return jsonify({'ok':False,'error':'sem_no must be 1..8'}),400
        start=(data.get('start_date') or '').strip(); end=(data.get('end_date') or '').strip()
        if not start or not end: return jsonify({'ok':False,'error':'Start/end date required'}),400
        if not _parse_iso_date(start) or not _parse_iso_date(end): return jsonify({'ok':False,'error':'Invalid date'}),400
        if _parse_iso_date(start)>_parse_iso_date(end): return jsonify({'ok':False,'error':'Start date cannot be after end date'}),400
        label=_norm(data.get('label')) or batch_sem_label(by,sn)
        ensure_batch_semesters(by, actor=who)
        conn=_db(); c=conn.cursor()
        try:
            c.execute("""INSERT INTO semesters(label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto)
                         VALUES (?,?,?,0,?,?,?, ?,?,0)
                         ON CONFLICT(batch_start_year,sem_no) WHERE batch_start_year IS NOT NULL AND sem_no IS NOT NULL
                         DO UPDATE SET label=excluded.label,start_date=excluded.start_date,end_date=excluded.end_date,updated_at=excluded.updated_at,is_auto=0""",
                      (label,start,end,who,now,now,by,sn))
            conn.commit()
            c.execute("SELECT id FROM semesters WHERE batch_start_year=? AND sem_no=?",(by,sn))
            row=c.fetchone(); sid=row[0] if row else None
        finally:
            conn.close()
        audit_log('semesters',str(sid),'save',None,{'batch_start_year':by,'sem_no':sn,'label':label,'start_date':start,'end_date':end},details='Admin saved batch semester')
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        today=_today_ist_str(); _,cur_no=visible_sem_nos('admin',by,on_date=today)
        rows=[_semester_json(s,batch_start_year=by,on_date=today) for s in sem_rows]
        cur_id=next((r['id'] for r in rows if int(r.get('sem_no') or 0)==int(cur_no)), None)
        return jsonify({'ok':True,'id':sid,'batch_start_year':by,'batch_label':batch_label(by),'current_sem_no':cur_no,'current_id':cur_id,'semesters':rows})
    # legacy global save (compat)
    label=_norm(data.get('label'))
    start=(data.get('start_date') or '').strip(); end=(data.get('end_date') or '').strip()
    is_current=str(data.get('is_current') or '').lower() in ('1','true','on','yes')
    if not label: return jsonify({'ok':False,'error':'Semester label is required'}),400
    if start and not _parse_iso_date(start): return jsonify({'ok':False,'error':'Invalid start date'}),400
    if end and not _parse_iso_date(end): return jsonify({'ok':False,'error':'Invalid end date'}),400
    if start and end and _parse_iso_date(start)>_parse_iso_date(end): return jsonify({'ok':False,'error':'Start date cannot be after end date'}),400
    conn=_db(); c=conn.cursor()
    sid=data.get('id')
    try: sid=int(sid) if sid not in (None,'','0') else None
    except Exception: sid=None
    if is_current: c.execute('UPDATE semesters SET is_current=0 WHERE batch_start_year IS NULL')
    if sid:
        c.execute('UPDATE semesters SET label=?,start_date=?,end_date=?,is_current=?,updated_at=? WHERE id=? AND batch_start_year IS NULL',
                  (label,start,end,1 if is_current else 0,now,sid))
    else:
        c.execute('INSERT INTO semesters (label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto) VALUES (?,?,?,?,?,?,?,NULL,NULL,0)',
                  (label,start,end,1 if is_current else 0,who,now,now))
        sid=c.lastrowid
    if not is_current:
        c.execute('SELECT COUNT(*) FROM semesters WHERE is_current=1 AND batch_start_year IS NULL'); has=int(c.fetchone()[0] or 0)
        if not has: c.execute('UPDATE semesters SET is_current=1 WHERE id=?',(sid,))
    conn.commit(); conn.close()
    _sync_current_semester_settings()
    audit_log('semesters',str(sid),'save',None,{'label':label,'start_date':start,'end_date':end,'is_current':is_current},details='Admin saved legacy semester')
    return jsonify({'ok':True,'id':sid,'semesters':[ _semester_json(s) for s in _semester_rows() ]})

@app.route('/api/admin/semesters/reset_defaults', methods=['POST'])
@admin_required
def api_admin_semesters_reset_defaults():
    data=request.get_json(silent=True) or {}
    try: by=int(data.get('batch_start_year'))
    except Exception: return jsonify({'ok':False,'error':'batch_start_year required'}),400
    sem_no=data.get('sem_no')
    who=session.get('display_name') or session.get('username') or 'admin'; now=_now_iso()
    conn=_db(); c=conn.cursor()
    try:
        targets=[int(sem_no)] if sem_no not in (None,'') else list(range(1,9))
        for sn in targets:
            start,end=default_batch_sem_dates(by,sn); label=batch_sem_label(by,sn)
            c.execute("""INSERT INTO semesters(label,start_date,end_date,is_current,created_by,created_at,updated_at,batch_start_year,sem_no,is_auto)
                         VALUES (?,?,?,0,?,?,?, ?,?,1)
                         ON CONFLICT(batch_start_year,sem_no) WHERE batch_start_year IS NOT NULL AND sem_no IS NOT NULL
                         DO UPDATE SET label=excluded.label,start_date=excluded.start_date,end_date=excluded.end_date,updated_at=excluded.updated_at,is_auto=1""",
                      (label,start,end,who,now,now,by,sn))
        conn.commit()
    finally:
        conn.close()
    audit_log('semesters',f'{by}','reset_defaults',None,{'batch_start_year':by,'sem_no':sem_no},details='Admin reset batch semester defaults')
    today=_today_ist_str(); ensure_batch_semesters(by,actor=who)
    rows=[_semester_json(s,batch_start_year=by,on_date=today) for s in _semester_rows(batch_start_year=by,include_legacy=False)]
    _,cur_no=visible_sem_nos('admin',by,on_date=today)
    cur_id=next((r['id'] for r in rows if int(r.get('sem_no') or 0)==int(cur_no)), None)
    return jsonify({'ok':True,'batch_start_year':by,'batch_label':batch_label(by),'current_sem_no':cur_no,'current_id':cur_id,'semesters':rows})

@app.route('/api/admin/semesters/<int:sid>', methods=['DELETE'])
@admin_required
def api_admin_semesters_delete(sid):
    conn=_db(); c=conn.cursor()
    c.execute('SELECT is_current,batch_start_year,sem_no FROM semesters WHERE id=?',(sid,)); row=c.fetchone()
    if not row: conn.close(); return jsonify({'ok':False,'error':'Semester not found'}),404
    was_current=int(row[0] or 0); by=row[1]; sn=row[2]
    # don't delete batch-sem rows; reset to default instead
    if by is not None and sn is not None:
        conn.close()
        return api_admin_semesters_reset_defaults() if False else jsonify({'ok':False,'error':'Batch semester rows cannot be deleted; use Reset to default.'}),400
    c.execute('DELETE FROM semesters WHERE id=?',(sid,))
    if was_current:
        c.execute("SELECT id FROM semesters WHERE batch_start_year IS NULL ORDER BY COALESCE(start_date,'') DESC, id DESC LIMIT 1"); nxt=c.fetchone()
        if nxt: c.execute('UPDATE semesters SET is_current=1 WHERE id=?',(nxt[0],))
    conn.commit(); conn.close()
    _sync_current_semester_settings()
    audit_log('semesters',str(sid),'delete',None,None,details='Admin deleted legacy semester')
    return jsonify({'ok':True,'semesters':[ _semester_json(s) for s in _semester_rows() ]})

@app.route('/api/admin/semesters/rollover/preview', methods=['POST'])
@admin_required
def api_admin_semesters_rollover_preview():
    data=request.get_json(silent=True) or {}
    try: by=int(data.get('batch_start_year'))
    except Exception: return jsonify({'ok':False,'error':'batch_start_year required'}),400
    to_sem=data.get('to_sem_no')
    return jsonify(_rollover_preview(by,to_sem))

@app.route('/api/admin/semesters/rollover/apply', methods=['POST'])
@admin_required
def api_admin_semesters_rollover_apply():
    data=request.get_json(silent=True) or {}
    try: by=int(data.get('batch_start_year'))
    except Exception: return jsonify({'ok':False,'error':'batch_start_year required'}),400
    to_sem=data.get('to_sem_no')
    actor=session.get('display_name') or session.get('username') or 'admin'
    res=apply_batch_rollover(by,to_sem,actor=actor)
    return jsonify(res)

@app.route('/admin/calendar')
@admin_required
def admin_calendar_page():
    batches=[{'batch_start_year':by,'label':batch_label(by)} for by in BATCH_START_YEARS]
    return render_template('admin_calendar.html', user=_current_user(), batches=batches, today=_today_ist_str())

@app.route('/api/admin/calendar', methods=['GET'])
@admin_required
def api_admin_calendar_list():
    return jsonify({'ok':True,'items':_calendar_items(None, request.args.get('from'), request.args.get('to'))})

@app.route('/api/admin/calendar', methods=['POST'])
@admin_required
def api_admin_calendar_add():
    title=_norm(request.form.get('title'))
    date=(request.form.get('date') or '').strip()
    end_date=(request.form.get('end_date') or '').strip() or None
    typ=(request.form.get('type') or 'note').strip()
    affects=1 if (request.form.get('affects_attendance') in ('1','true','on','yes',None) and request.form.get('affects_attendance')!='0') else 0
    if request.form.get('affects_attendance')=='0': affects=0
    class_name=(request.form.get('class_name') or '').strip() or None
    notes=_norm(request.form.get('notes'))
    if not title or not date: return jsonify({'ok':False,'error':'Title and date are required'}), 400
    start_date=_parse_iso_date(date)
    if not start_date: return jsonify({'ok':False,'error':'Invalid date'}), 400
    if end_date:
        parsed_end=_parse_iso_date(end_date)
        if not parsed_end: return jsonify({'ok':False,'error':'Invalid end date'}), 400
        if parsed_end<start_date: return jsonify({'ok':False,'error':'End date cannot be before the start date'}), 400
    now=datetime.datetime.utcnow().isoformat(); who=_current_user().get('display_name') or session.get('username')
    conn=_db(); c=conn.cursor()
    c.execute('INSERT INTO academic_calendar (title,date,end_date,type,affects_attendance,class_name,notes,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
              (title,date,end_date,typ,affects,class_name,notes,who,now))
    cid=c.lastrowid; conn.commit(); conn.close()
    return jsonify({'ok':True,'id':cid})

@app.route('/api/admin/calendar/<int:cid>', methods=['DELETE'])
@admin_required
def api_admin_calendar_delete(cid):
    conn=_db(); c=conn.cursor(); c.execute('DELETE FROM academic_calendar WHERE id=?',(cid,)); conn.commit(); n=c.rowcount; conn.close()
    return jsonify({'ok':True,'deleted':int(n>0)})

@app.route('/api/student/dashboard')
@student_required
def api_student_dashboard():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile found.'}), 404
    st={'id':student[0],'name':student[1],'roll':student[2],'class':student[3],'section':student[4],'reg_no':student[5],'dob':student[7] if len(student)>7 else None}
    class_name=(student[3] or '').strip()
    now_ist=_now_ist(); today=now_ist.date()
    qfrom=request.args.get('from'); qto=request.args.get('to'); qsem=request.args.get('semester_id')
    sem=_semester_by_id(qsem) if qsem else None
    sem_no_raw=request.args.get('sem_no')
    by_raw=request.args.get('batch_start_year')
    info=parse_cohort_class(class_name)
    by_hint=by_raw if by_raw not in (None,'') else (info.get('batch_start_year') if info else None)
    if by_hint is not None:
        try: ensure_batch_semesters(int(by_hint), actor=session.get('username') or 'system')
        except Exception: pass
    if not sem and sem_no_raw not in (None,''):
        try:
            if by_hint is not None: sem=_batch_semester_row(int(by_hint), int(sem_no_raw))
        except Exception: sem=None
    if not sem and not (qfrom and qto):
        info=parse_cohort_class(class_name)
        by=info.get('batch_start_year') if info else None
        if by is not None:
            cur_no=current_sem_for_batch(by, on_date=today.isoformat())
            sem=_batch_semester_row(by, cur_no)
        else:
            sem=_current_semester()
    if sem:
        d1,d2=_semester_window(sem, class_name, today)
        sem_meta={'id':sem.get('id'),'label':sem.get('label') or 'Semester','from':d1.isoformat(),'to':d2.isoformat(),
                  'is_current':int(sem.get('is_current') or 0),'sem_no':sem.get('sem_no'),'batch_start_year':sem.get('batch_start_year')}
    else:
        d1=_parse_iso_date(qfrom); d2=_parse_iso_date(qto)
        conn0=_db(); c0=conn0.cursor()
        try:
            if not d1 or not d2:
                c0.execute("SELECT MIN(s.date), MAX(s.date) FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id WHERE e.student_id=?", (student[0],))
                r=c0.fetchone() or (None,None)
                if not d1: d1=_parse_iso_date(r[0])
                if not d2: d2=_parse_iso_date(r[1])
        except Exception: pass
        conn0.close()
        if not d2: d2=today
        if not d1: d1=d2-datetime.timedelta(days=180)
        sem_meta={'id':None,'label':(get_setting('semester_label','') or '').strip() or 'Custom range','from':d1.isoformat(),'to':d2.isoformat(),'is_current':0}
    if d1>d2: d1,d2=d2,d1
    if (d2-d1).days>366: d1=d2-datetime.timedelta(days=366)
    sem_meta['from']=d1.isoformat(); sem_meta['to']=d2.isoformat()
    conn=_db(); c=conn.cursor()
    rows=[]
    try:
        sql = ("SELECT s.date,s.period,s.subject,s.subject_display,s.class_name,s.submitted_at,s.source,s.id,"
               " e.status,e.source,e.marked_by,e.updated_at,e.confidence,e.flagged,e.id,e.elective_label"
               " FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"
               " WHERE e.student_id=? AND s.date>=? AND s.date<=? ORDER BY s.date DESC, s.period ASC")
        c.execute(sql, (student[0], d1.isoformat(), d2.isoformat()))
        rows=c.fetchall()
    except Exception:
        conn.close(); return jsonify({'ok':False,'error':'Could not load dashboard data'}), 500
    emap={}
    try:
        c.execute('SELECT slot_key,option_label FROM student_electives WHERE student_id=? AND upper(class_name)=upper(?) AND timetable_version_id IS NULL',(student[0],class_name))
        emap={str(slot).upper():label for slot,label in c.fetchall()}
    except Exception:
        emap={}
    emaps=_elective_lookup()
    conn.close()
    tt=load_timetable(class_name) if class_name else None
    if tt:
        tt=dict(tt); tt['schedule']=_apply_elective_map_to_schedule(tt.get('schedule') or {}, emap)
    sched=(tt or {}).get('schedule') or {}
    cal_map=_calendar_map_for_range(class_name, d1, d2)
    entry_by_date={}
    for r in rows:
        (date, period, subj, subj_disp, cls, submitted_at, sess_src, sess_id,
         status, esrc, marked_by, updated_at, confidence, flagged, eid, entry_label)=r
        stt=_normalize_attendance_status(status)
        try: pd=int(period or 0)
        except Exception: pd=0
        esource=(esrc or sess_src or 'manual')
        entry={'session_id':sess_id,'entry_id':eid,'date':date,'period':pd,
               'subject':subj or '','subject_display':subj_disp or subj or f"P{pd}",
               'class_name':cls or class_name,'status':stt,
               'source':esource,'session_source':sess_src or '',
               'marked_by':marked_by or '','updated_at':updated_at or submitted_at or '',
               'submitted_at':submitted_at or '','confidence':confidence,'flagged':int(flagged or 0),
               'scheduled':True,'default_absent':esource=='auto_absent','unmarked':stt==UNMARKED_STATUS,
               'method_label':('Unmarked period (no explicit mark)' if stt==UNMARKED_STATUS else ('Medical leave workflow (Advisor+HOD approved)' if esource=='medical_workflow' else ('OD workflow (Advisor+HOD approved)' if esource=='od_workflow' else 'Staff period submission (manual)'))),
               'confidence_label':(f"{round(float(confidence)*100,1)}%" if confidence is not None else '')}
        mapped=(entry_label or '').strip() or _effective_subject(emaps,student[0],cls or class_name,date,pd,emap.get(f"{_day_key(date)}-P{pd}") or '')
        if mapped:
            entry['subject']=mapped; entry['subject_display']=mapped; entry['selected_elective']=mapped
        entry_by_date.setdefault(date,{})[pd]=entry
    if sched:
        day_dates=[]; cur=d1
        while cur<=d2:
            ds=cur.isoformat(); dk=_day_key(ds); day_cal=cal_map.get(ds,[]) or []
            holiday=any((str(it.get('type') or '').lower()=='holiday' and int(it.get('affects_attendance') or 0)==1) for it in day_cal)
            override=any((str(it.get('type') or '').lower()=='working_day_override') for it in day_cal)
            has=(bool(sched.get(dk)) and (not holiday or override)) or (ds in entry_by_date)
            if has: day_dates.append(cur)
            cur += datetime.timedelta(days=1)
    else:
        day_dates=sorted([_parse_iso_date(x) for x in entry_by_date if _parse_iso_date(x)])
        day_dates=[d for d in day_dates if d1<=d<=d2]
    grid=[]; days_present=set(); absent_items=[]; emap_cache={}
    for d in day_dates:
        ds=d.isoformat(); dk=_day_key(ds); day_cal=cal_map.get(ds,[]) or []
        holiday=any((str(it.get('type') or '').lower()=='holiday' and int(it.get('affects_attendance') or 0)==1) for it in day_cal)
        override=any((str(it.get('type') or '').lower()=='working_day_override') for it in day_cal)
        day_tt=load_timetable(class_name,ds) or tt
        day_vid=_timetable_version_id(class_name,ds)
        cache_key=day_vid if day_vid is not None else 'legacy'
        if cache_key not in emap_cache:
            emap_cache[cache_key]=_student_elective_map(student[0],class_name,day_vid) if day_vid is not None else emap
        day_emap=emap_cache.get(cache_key) or {}
        if day_emap and day_tt:
            day_tt=dict(day_tt); day_tt['schedule']=_apply_elective_map_to_schedule(day_tt.get('schedule') or {},day_emap)
        sched_list=[] if (holiday and not override) else ((day_tt or {}).get('schedule') or {}).get(dk,[])
        sched_map={str(cel.get('period')):cel for cel in sched_list}
        ents=entry_by_date.get(ds,{})
        obj={'date':ds,'day':dk,'periods':{},'calendar':day_cal,'scheduled_periods':0}
        for p in range(1,8):
            cel=sched_map.get(str(p)); ent=ents.get(p)
            if ent:
                c2=dict(ent); c2['scheduled']=True
                if not c2.get('class_name'): c2['class_name']=class_name
                c2.setdefault('slot', f"{((cel or {}).get('start') or '')}-{((cel or {}).get('end') or '')}".strip('-'))
                obj['periods'][str(p)]=c2; obj['scheduled_periods']+=1
                if _is_attendance_credited(c2.get('status')): days_present.add(ds)
                if _is_attendance_absence(c2.get('status')): absent_items.append({'date':ds,'period':p,'subject':c2.get('subject_display') or '', 'status':c2.get('status'),'marked_by':c2.get('marked_by') or '','source':c2.get('source') or '','default_absent':(c2.get('source')=='auto_absent')})
            elif cel:
                due=_unmarked_placeholder_due(ds,now_ist)
                stt=UNMARKED_STATUS if due else 'Not Marked'
                item={'date':ds,'period':p,'status':stt,'scheduled':True,'class_name':class_name,
                      'subject':cel.get('subject') or '','subject_display':cel.get('display') or cel.get('subject') or f'P{p}',
                      'source':'auto_absent' if due else None,'session_source':'','marked_by':'','updated_at':'','submitted_at':'',
                      'confidence':None,'confidence_label':'',
                      'slot':f"{cel.get('start') or ''}-{cel.get('end') or ''}".strip('-'),
                      'method_label':('Unmarked period (no explicit mark)' if due else 'Not yet due'),
                      'unmarked':due,'scheduled':True}
                obj['periods'][str(p)]=item; obj['scheduled_periods']+=1
            else:
                obj['periods'][str(p)]={'date':ds,'period':p,'status':'NA','scheduled':False,'class_name':class_name}
        if obj['scheduled_periods']: grid.append(obj)
    grid.sort(key=lambda x:x['date'], reverse=True)
    present_p2=late_p2=od_p2=medical_p2=absent_p2=n_p2=auto_absent_p2=unmarked_p2=not_marked_p2=0; scheduled_periods=0
    subject_stats={}
    for g in grid:
        for p in range(1,8):
            c2=g['periods'].get(str(p)) or {}
            if not c2.get('scheduled'): continue
            scheduled_periods+=1
            stt=_normalize_attendance_status(c2.get('status'))
            if stt=='Present': present_p2+=1
            elif stt=='Late': late_p2+=1
            elif stt=='OD': od_p2+=1
            elif stt=='Medical Leave': medical_p2+=1
            elif stt=='N': n_p2+=1; absent_p2+=1
            elif stt=='Absent': absent_p2+=1
            elif stt==UNMARKED_STATUS:
                unmarked_p2+=1; not_marked_p2+=1
                if c2.get('source')=='auto_absent': auto_absent_p2+=1
            elif stt=='Not Marked': not_marked_p2+=1
            if _is_attendance_counted(stt):
                subject=(c2.get('subject_display') or c2.get('subject') or 'Unspecified').strip()
                stat=subject_stats.setdefault(subject,{'subject':subject,'total':0,'credited':0,'present':0,'late':0,'od':0,'medical':0,'absent':0})
                stat['total']+=1
                if _is_attendance_credited(stt): stat['credited']+=1
                if stt=='Present': stat['present']+=1
                elif stt=='Late': stat['late']+=1
                elif stt=='OD': stat['od']+=1
                elif stt=='Medical Leave': stat['medical']+=1
                elif stt in ('Absent','N'): stat['absent']+=1
    working_days=sum(1 for g in grid if any(_is_attendance_counted((g['periods'].get(str(p)) or {}).get('status')) for p in range(1,8)))
    days_present_n=len(days_present); days_absent=max(0, working_days-days_present_n)
    total_marked=present_p2+late_p2+od_p2+medical_p2+absent_p2; attended=present_p2+late_p2+od_p2+medical_p2
    pct=round((attended/total_marked*100),2) if total_marked else 0.0
    try:
        threshold=float(get_setting('defaulter_threshold','75') or 75)
    except Exception:
        threshold=75.0
    can_miss=0
    if threshold>0 and total_marked:
        can_miss=max(0,int(attended*100.0/threshold)-total_marked)
    subjects=[]
    for stat in subject_stats.values():
        stat['percentage']=round((stat['credited']/(stat['total'] or 1))*100,2)
        subjects.append(stat)
    subjects.sort(key=lambda x:(x['percentage'],x['subject']))
    grid_asc=sorted(grid,key=lambda x:x['date'])
    today_classes=[]
    for g in grid_asc:
        if g['date']!=today.isoformat(): continue
        for p in range(1,8):
            item=g['periods'].get(str(p)) or {}
            if item.get('scheduled'):
                today_classes.append({'period':p,'subject':item.get('subject_display') or item.get('subject') or f'P{p}',
                                      'status':item.get('status') or '', 'slot':item.get('slot') or ''})
    next_class=None
    for g in grid_asc:
        if g['date']<=today.isoformat() or not g.get('scheduled_periods'): continue
        periods=[]
        for p in range(1,8):
            item=g['periods'].get(str(p)) or {}
            if item.get('scheduled'):
                periods.append({'period':p,'subject':item.get('subject_display') or item.get('subject') or f'P{p}',
                                'slot':item.get('slot') or ''})
        if periods:
            next_class={'date':g['date'],'day':g.get('day') or '','periods':periods}
            break
    overview={'semester_id':sem_meta['id'],'semester_label':sem_meta['label'],'semester_name':sem_meta['label'],
              'is_current':sem_meta['is_current'],'from':sem_meta['from'],'to':sem_meta['to'],
              'working_days':working_days,'days_present':days_present_n,'days_absent':days_absent,
              'scheduled_periods':scheduled_periods,'total_periods_marked':total_marked,'not_marked_periods':not_marked_p2,
               'present_periods':present_p2,'late_periods':late_p2,'od_periods':od_p2,'medical_periods':medical_p2,
               'absent_periods':absent_p2,'n_periods':n_p2,'unmarked_periods':unmarked_p2,
               'auto_absent_periods':auto_absent_p2,'attendance_percentage':pct,
               'threshold':threshold,'can_miss':can_miss}
    cal_up=[it for it in _calendar_items(class_name, today.isoformat(), (today+datetime.timedelta(days=60)).isoformat())][:20]
    try:
        tt=tt or {}
        timetable={'slots':sorted((tt.get('slots') or []), key=lambda x:int(x.get('period') or 0)), 'schedule':tt.get('schedule') or {}, 'advisor':tt.get('class_advisor') or {}, 'class_name':class_name}
    except Exception:
        timetable={}
    info=parse_cohort_class(class_name)
    by=info.get('batch_start_year') if info else None
    sem_rows=_semester_rows(batch_start_year=by, include_legacy=(by is None))
    if by is not None:
        ensure_batch_semesters(by, actor=session.get('username') or 'system')
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        visible,_cur_no=visible_sem_nos('student',by,on_date=today.isoformat())
        sem_rows=[s for s in sem_rows if int(s.get('sem_no') or 0) in set(visible)]
    sem_list=[_semester_json(s,batch_start_year=by,on_date=today.isoformat()) for s in sem_rows]
    absent_items.sort(key=lambda x:(x['date'],x['period']), reverse=True)
    export_qs={'semester_id':sem_meta['id']} if sem_meta.get('id') else {'from':d1.isoformat(),'to':d2.isoformat()}
    if sem_meta.get('id'):
        sm=_semester_by_id(sem_meta['id'])
        if sm and sm.get('sem_no') is not None: export_qs['sem_no']=sm.get('sem_no')
        if sm and sm.get('batch_start_year') is not None: export_qs['batch_start_year']=sm.get('batch_start_year')
    return jsonify({'ok':True,'student':st,'overview':overview,'semester':sem_meta,'semesters':sem_list,
                    'grid':grid,'absent_audit':absent_items[:500],'calendar_upcoming':cal_up,'timetable':timetable,'periods':list(range(1,8)),
                    'subjects':subjects,'today_classes':today_classes,'next_class':next_class,
                    'today_ist':today.isoformat(),
                    'advisor_contact':_advisor_contact_for_class(class_name),
                    'export_url':'/student/export_csv?'+urlencode(export_qs),
                     'note':'Working days and percentages exclude unmarked "-" periods. N counts as an absence. Medical Leave is credited like Present/Late/OD. Future periods stay Not Marked.'})

@app.route('/student/export_csv')
@student_required
def student_export_csv():
    student=_resolve_student_for_user()
    if not student: abort(404)
    class_name=(student[3] or '').strip(); today=_now_ist().date()
    qsem=request.args.get('semester_id'); qfrom=request.args.get('from'); qto=request.args.get('to')
    sem=_semester_by_id(qsem) if qsem else None
    sem_no_raw=request.args.get('sem_no'); by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,''):
        info=parse_cohort_class(class_name)
        by=by_raw if by_raw not in (None,'') else (info.get('batch_start_year') if info else None)
        try:
            if by is not None: sem=_batch_semester_row(int(by), int(sem_no_raw))
        except Exception: sem=None
    if sem:
        d1,d2=_semester_window(sem,class_name,today)
    else:
        d1=_parse_iso_date(qfrom); d2=_parse_iso_date(qto)
        if not d1 or not d2:
            info=parse_cohort_class(class_name); by=info.get('batch_start_year') if info else None
            if by is not None: sem=_batch_semester_row(by, current_sem_for_batch(by,on_date=today.isoformat()))
            else: sem=_current_semester()
            if sem:
                d1,d2=_semester_window(sem,class_name,today)
            else:
                conn0=_db(); c0=conn0.cursor()
                c0.execute("SELECT MIN(s.date),MAX(s.date) FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id WHERE e.student_id=?",(student[0],))
                r=c0.fetchone() or (None,None); conn0.close()
                d1=_parse_iso_date(r[0]) or today; d2=_parse_iso_date(r[1]) or today
    if d1>d2: d1,d2=d2,d1
    conn=_db(); c=conn.cursor()
    c.execute("""SELECT s.date,s.period,COALESCE(NULLIF(e.elective_label,''),s.subject_display,s.subject),e.status,e.source,e.marked_by,e.updated_at
                 FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id
                 WHERE e.student_id=? AND s.date>=? AND s.date<=? ORDER BY s.date,s.period""",
              (student[0],d1.isoformat(),d2.isoformat()))
    rows=c.fetchall(); conn.close()
    output=io.StringIO()
    output.write('Date,Period,Subject,Status,Source,MarkedBy,Updated\n')
    for r in rows:
        output.write(','.join('"'+str(x or '').replace('"','""')+'"' for x in r)+'\n')
    mem=io.BytesIO(output.getvalue().encode('utf-8-sig')); mem.seek(0)
    return send_file(mem,as_attachment=True,download_name=f"my_attendance_{d1.isoformat()}_{d2.isoformat()}.csv",mimetype='text/csv')

# ERP_OD_WORKFLOW
OD_SCOPE_TYPES = ('full_day','half_day_1_4','half_day_5_7','specific')

def _save_od_proof(file_storage):
    if not file_storage or not file_storage.filename: return None,None
    fname=secure_filename(file_storage.filename)
    ext=os.path.splitext(fname)[1].lower()
    save=f"od_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext or '.bin'}"
    path=os.path.join(OD_PROOFS_DIR, save)
    file_storage.save(path)
    return path, fname

def _advisor_for_class(class_name):
    tt=load_timetable((class_name or '').strip())
    if tt and (tt.get('class_advisor') or {}).get('initials'):
        return ((tt['class_advisor']['initials'] or '').upper(), tt['class_advisor'].get('name') or '')
    # fallback known mapping
    m={'24IT5A':('KB','Ms. K. Baby'),'24IT5B':('TK','Ms. T. Kokilavani'),'23IT7A':('','')}
    return m.get((class_name or '').upper(), (None,None))

def _normalize_od_scope(scope_type, periods_json, half):
    scope=(scope_type or 'full_day').strip()
    periods=[]
    if scope=='half_day_1_4' or half=='1-4': scope='half_day_1_4'; periods=[1,2,3,4]
    elif scope=='half_day_5_7' or half=='5-7': scope='half_day_5_7'; periods=[5,6,7]
    elif scope=='specific' or periods_json:
        scope='specific'
        raw=(periods_json or '')
        for part in re.split(r'[\s,]+', str(raw)):
            if not part: continue
            try:
                v=int(part)
                if 1<=v<=7 and v not in periods: periods.append(v)
            except Exception: pass
        periods.sort()
        if not periods: periods=[1,2,3,4,5,6,7]; scope='full_day'
    else:
        scope='full_day'; periods=[1,2,3,4,5,6,7]
    return scope, periods

def _od_scope_label(scope, periods):
    if scope=='half_day_1_4': return 'Half Day (Periods 1-4)'
    if scope=='half_day_5_7': return 'Half Day (Periods 5-7)'
    if scope=='specific': return 'Specific Periods (' + ','.join(map(str,periods)) + ')'
    return 'Full Day (All periods 1-7)'

def _od_hod_display(req):
    # Phase B: if advisor rejected (explicit/auto) show HOD as NIL
    if (req.get('advisor_decision') or '').lower() == 'rejected':
        return 'NIL'
    return (req.get('hod_decision') or 'NIL')

def _expire_od_requests_if_needed(conn=None):
    """Phase B (request-driven): OD has no submission time regulation, but if not fully approved (od_approved)
    by to_date 18:00:00 IST, auto-cancel as advisor rejected; HOD shown as NIL. For ranges, use to_date only."""
    own = conn is None
    if own: conn = _db()
    c = conn.cursor()
    now = _now_ist(); today = now.date(); now_naive = now.replace(tzinfo=None)
    changed = 0; expired=[]
    try:
        c.execute("SELECT id,to_date,status,student_id,class_name FROM od_requests WHERE status IN ('pending_advisor','advisor_approved')")
        for rid, to_date, st, student_id, class_name in c.fetchall():
            try:
                td = datetime.date.fromisoformat((to_date or '').strip())
            except Exception:
                continue
            deadline = datetime.datetime.combine(td, datetime.time(18, 0, 0))
            if now_naive >= deadline:
                ts = datetime.datetime.utcnow().isoformat()
                note = f"Auto-rejected: not approved by 6:00 PM IST on {td.isoformat()}"
                c.execute("""UPDATE od_requests SET status='cancelled', advisor_decision='rejected', advisor_note=?,
                             advisor_at=?, hod_decision='NIL', hod_note=NULL, hod_at=NULL, updated_at=?
                             WHERE id=? AND status IN ('pending_advisor','advisor_approved')""",
                          (note, ts, ts, rid))
                if c.rowcount:
                    changed += c.rowcount
                    expired.append((student_id, class_name, rid, td.isoformat()))
    except Exception:
        pass
    if own:
        conn.commit(); conn.close()
        for student_id, class_name, rid, td in expired:
            if student_id:
                _enqueue_alert(student_id, 'od_expired', 'OD request auto-rejected',
                               f"Your OD request for {class_name} on {td} was auto-rejected because it was not fully approved by 6:00 PM IST.",
                               event_key='od_rejected', source_ref=f'od:{rid}')
    return changed

def _hod_cancel_od_allowed(to_date_str):
    # HOD may cancel OD until OD gets over: allow while now_ist <= to_date 23:59:59 IST (inclusive of last day)
    try:
        td = datetime.date.fromisoformat((to_date_str or '').strip())
    except Exception:
        return False
    now = _now_ist().replace(tzinfo=None)
    end = datetime.datetime.combine(td, datetime.time(23, 59, 59))
    return now <= end

def _revert_cancelled_od(conn, req):
    """Phase B: if HOD cancels OD, override OD effect back to unmarked '-'; keep explicit Present marks not from this OD workflow."""
    c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    rid = req['id']; sid = req['student_id']
    periods = _parse_req_periods(req)
    try:
        d1 = datetime.date.fromisoformat(req['from_date']); d2 = datetime.date.fromisoformat(req['to_date'])
    except Exception:
        return 0
    # remove OD records issued for this request
    try:
        c.execute("DELETE FROM od_records WHERE request_id=?", (rid,))
    except Exception:
        pass
    reverted = 0
    cur = d1
    while cur <= d2:
        for per in periods:
            try:
                c.execute("""SELECT e.id, e.status, e.source FROM attendance_entries e
                             JOIN attendance_sessions s ON s.id=e.session_id
                             WHERE e.student_id=? AND s.class_name=? AND s.date=? AND s.period=?""",
                          (sid, req['class_name'], cur.isoformat(), per))
                row = c.fetchone()
                if not row: continue
                eid, st, src = row
                # keep explicit non-OD Present (e.g., camera/manual present), revert OD-workflow OD/absent provenance to Absent
                if st == 'Present' and src not in ('od_workflow', 'od_cancel_revert'):
                    continue
                if st == 'OD' or src == 'od_workflow' or src == 'od_cancel_revert':
                    c.execute("UPDATE attendance_entries SET status=?, source='od_cancel_revert', marked_by=?, updated_at=? WHERE id=?",
                              (UNMARKED_STATUS, 'HOD cancelled OD', now, eid))
                    reverted += 1
            except Exception:
                pass
        cur += datetime.timedelta(days=1)
    return reverted

@app.route('/od')
@login_required
def od_page():
    _expire_od_requests_if_needed()
    role=session.get('role'); student=_resolve_student_for_user() if role=='student' else None
    return render_template('od.html', user=_current_user(), student=student, classes=get_classes(), today=_today_ist_str())

@app.route('/api/od/request', methods=['POST'])
@student_required
def api_od_request():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'Student profile not linked'}), 404
    reason=(request.form.get('reason') or '').strip()
    from_date=(request.form.get('from_date') or '').strip(); to_date=(request.form.get('to_date') or from_date).strip()
    scope_type=(request.form.get('scope_type') or request.form.get('od_scope') or 'full_day').strip()
    half=(request.form.get('half_day') or '').strip()
    periods_json=(request.form.get('periods_json') or '').strip() or None
    scope, periods = _normalize_od_scope(scope_type, periods_json, half)
    if not reason or not from_date: return jsonify({'ok':False,'error':'Reason and date are required'}), 400
    try:
        d1=datetime.date.fromisoformat(from_date); d2=datetime.date.fromisoformat(to_date)
        if d2 < d1: d2=d1
        from_date,to_date=d1.isoformat(),d2.isoformat()
    except Exception:
        return jsonify({'ok':False,'error':'Invalid date'}), 400
    class_name=(student[3] or '').strip()
    advisor_code, advisor_name = _advisor_for_class(class_name)
    if not advisor_code:
        return jsonify({'ok':False,'error':f'Class advisor not found for class "{class_name}". Ask admin to set correct class in YYIT<sem><section> format.'}), 400
    proof_path, proof_name = _save_od_proof(request.files.get('proof'))  # optional
    now=datetime.datetime.utcnow().isoformat()
    day_wise = 1 if scope=='full_day' else 0
    scope_label=_od_scope_label(scope, periods)
    conn=_db(); c=conn.cursor()
    c.execute("INSERT INTO od_requests (student_id,reg_no,student_name,class_name,from_date,to_date,periods_json,day_wise,reason,proof_path,proof_name,status,advisor_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending_advisor', ?, ?, ?)",
              (student[0], student[5] or '', student[1] or '', class_name, from_date, to_date, json.dumps(periods), day_wise, reason + f" [Scope: {scope_label}]", proof_path, proof_name, advisor_code, now, now))
    rid=c.lastrowid; conn.commit(); conn.close()
    audit_log('od_request', rid, 'submit', None, {'class':class_name,'from':from_date,'to':to_date,'scope':scope_label,'periods':periods}, details=reason)
    _enqueue_alert(student[0], 'od_submitted', 'OD request submitted',
                   f"Your OD request for {class_name} ({from_date} to {to_date}) was sent to advisor {advisor_code}.",
                   event_key='od_submitted', source_ref=f'od:{rid}')
    return jsonify({'ok':True,'request_id':rid,'status':'pending_advisor','scope':scope,'periods':periods,'advisor_code':advisor_code,
                    'message':f'OD request submitted to class advisor ({advisor_code}). Advisor must approve, then HOD must approve. If either rejects, OD is cancelled. Attendance will be marked for: {scope_label} on approved date(s).'})

@app.route('/api/od/my')
@student_required
def api_od_my():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':True,'requests':[]})
    _expire_od_requests_if_needed()
    conn=_db(); c=conn.cursor(); c.execute('SELECT id,from_date,to_date,day_wise,periods_json,reason,status,advisor_decision,advisor_note,advisor_at,hod_decision,hod_note,hod_at,proof_name,created_at,class_name,advisor_code FROM od_requests WHERE student_id=? ORDER BY id DESC LIMIT 200',(student[0],))
    rows=c.fetchall(); conn.close()
    keys=['id','from_date','to_date','day_wise','periods_json','reason','status','advisor_decision','advisor_note','advisor_at','hod_decision','hod_note','hod_at','proof_name','created_at','class_name','advisor_code']
    out=[]
    for r in rows:
        d=dict(zip(keys,r))
        st=d.get('status')
        if (d.get('advisor_decision') or '').lower() == 'rejected':
            d['hod_decision_display'] = 'NIL'
            d['hod_note'] = d.get('hod_note') or ''
        else:
            d['hod_decision_display'] = d.get('hod_decision') or ('NIL' if st not in ('od_approved','advisor_approved') else '-')
        if st=='pending_advisor': d['status_label']='Pending Class Advisor approval'
        elif st=='advisor_approved': d['status_label']='Advisor approved ? Awaiting HOD approval'
        elif st=='od_approved': d['status_label']='OD Approved (attendance marked)'
        elif st=='cancelled' and 'Auto-rejected' in (d.get('advisor_note') or ''):
            d['status_label']='Auto-rejected (not approved by 6:00 PM IST on last OD date)'
        elif st=='cancelled': d['status_label']='Rejected / Cancelled'
        else: d['status_label']=st
        out.append(d)
    return jsonify({'ok':True,'requests':out})

def _od_scope_ok_for_user(req_class):
    # Admin not involved in OD decisions per policy
    if session.get('role')=='admin': return False
    if session.get('is_hod'): return True
    if session.get('is_advisor') and session.get('advisor_class')==req_class: return True
    return False

def _can_download_od_proof(req_class):
    if session.get('role')=='admin': return False
    if session.get('is_hod'): return True
    if session.get('is_advisor') and session.get('advisor_class')==req_class: return True
    return False

@app.route('/api/od/pending')
@staff_required
def api_od_pending():
    _expire_od_requests_if_needed()
    role=session.get('role'); is_hod=bool(session.get('is_hod')); is_adv=bool(session.get('is_advisor')); adv_class=session.get('advisor_class')
    conn=_db(); c=conn.cursor()
    viewer_role='none'; qbase="SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,proof_name,advisor_decision,advisor_note,hod_decision,hod_note,created_at,advisor_code FROM od_requests "
    if role=='admin':
        conn.close(); return jsonify({'ok':True,'requests':[],'viewer_role':'admin','note':'Admin is not involved in OD approval. Class advisor and HOD handle OD requests.'})
    elif is_hod and not is_adv:
        viewer_role='hod'
        c.execute(qbase+"WHERE status IN ('advisor_approved','od_approved') ORDER BY (status='advisor_approved') DESC, id DESC LIMIT 300")
    elif is_hod and is_adv:
        viewer_role='hod_advisor'
        c.execute(qbase+"WHERE status IN ('advisor_approved','od_approved') OR (status='pending_advisor' AND class_name=?) ORDER BY id DESC LIMIT 300", (adv_class or '',))
    elif is_adv:
        viewer_role='advisor'
        c.execute(qbase+"WHERE class_name=? AND status='pending_advisor' ORDER BY id DESC LIMIT 300", (adv_class,))
    else:
        conn.close(); return jsonify({'ok':True,'requests':[],'viewer_role':'staff','note':'Only the class advisor and HOD can review OD requests. For a single handled period, consent/handling staff can assign OD directly in Take Attendance.'})
    rows=c.fetchall(); conn.close()
    role_map=_student_roles_map([r[1] for r in rows])
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json','reason','status','proof_name','advisor_decision','advisor_note','hod_decision','hod_note','created_at','advisor_code']
    reqs=[]
    for r in rows:
        d=dict(zip(keys,r))
        d['student_role']=role_map.get(int(r[1] or 0),'')
        acts=[]
        if viewer_role=='advisor' and d['status']=='pending_advisor' and d['class_name']==adv_class:
            acts=['advisor_approve','advisor_reject']
        elif viewer_role=='hod':
            if d['status']=='advisor_approved': acts=['hod_approve','hod_reject']
            elif d['status']=='od_approved' and _hod_cancel_od_allowed(d.get('to_date')): acts=['hod_cancel']
        elif viewer_role=='hod_advisor':
            if d['status']=='advisor_approved': acts=['hod_approve','hod_reject']
            elif d['status']=='od_approved' and _hod_cancel_od_allowed(d.get('to_date')): acts=['hod_cancel']
            elif d['status']=='pending_advisor' and d['class_name']==adv_class: acts=['advisor_approve','advisor_reject']
        d['allowed_actions']=acts
        d['hod_decision_display'] = _od_hod_display(d)
        reqs.append(d)
    return jsonify({'ok':True,'requests':reqs,'viewer_role':viewer_role,'advisor_class':adv_class})

def _parse_req_periods(req):
    raw=req.get('periods_json')
    periods=[]
    try:
        if raw:
            v=json.loads(raw) if isinstance(raw,str) else raw
            if isinstance(v, list):
                for x in v:
                    try:
                        n=int(x);
                        if 1<=n<=7 and n not in periods: periods.append(n)
                    except Exception: pass
    except Exception: pass
    if not periods:
        if req.get('day_wise'): periods=list(range(1,8))
        else: periods=list(range(1,8))
    return sorted(periods)

def _find_or_create_session(conn, class_name, date, period):
    c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
    tt=load_timetable(class_name,date) or {}
    cell=timetable_cell(class_name,date,period) or {}
    version=_timetable_version_row(class_name,date)
    version_id=int(version['id']) if version else None
    duration=_slot_duration_minutes(tt,period)
    disp=(cell.get('display') or cell.get('subject') or f'P{period}').strip(); subj=(cell.get('subject') or disp).strip()
    c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(class_name,date,period,disp))
    row=c.fetchone()
    if row:
        c.execute('UPDATE attendance_sessions SET timetable_version_id=COALESCE(timetable_version_id,?), duration_minutes=COALESCE(duration_minutes,?) WHERE id=?',(version_id,duration,row[0]))
        return row[0], disp
    c.execute('INSERT INTO attendance_sessions (class_name,date,period,subject,subject_display,staff_code,staff_name,status,source,created_at,submitted_at,notes,timetable_version_id,duration_minutes) VALUES (?,?,?,?,?,?,?, "submitted","od_workflow",?,?,?,?,?)',
              (class_name,date,period,subj,disp,'OD','Advisor+HOD',now,now,'Auto-created from approved OD request',version_id,duration))
    return c.lastrowid, disp

def _od_day_due(date_str, now=None):
    d=_parse_iso_date(date_str)
    if not d: return False
    now = now or _now_ist()
    if d < now.date(): return True
    if d > now.date(): return False
    return now.hour >= OD_APPLY_HOUR

def _od_day_is_working(class_name, date_str):
    hits=_calendar_conflict(class_name, date_str)
    holiday=any((str(it.get('type') or '').lower()=='holiday' and int(it.get('affects_attendance') or 0)==1) for it in hits)
    override=any((str(it.get('type') or '').lower()=='working_day_override') for it in hits)
    return (not holiday) or override

def _apply_od_for_date(conn, req, date_str, now=None):
    """Apply one approved OD request to one date. Never overrides explicit Present/Late."""
    if not _od_day_due(date_str, now): return 0
    if not _od_day_is_working(req.get('class_name'), date_str): return 0
    c=conn.cursor(); now_iso=datetime.datetime.utcnow().isoformat()
    periods=_parse_req_periods(req); marked=0
    emaps=_elective_lookup()
    for per in periods:
        cell=timetable_cell(req.get('class_name'), date_str, per)
        if not cell: continue
        disp=(cell.get('display') or cell.get('subject') or f'P{per}').strip()
        c.execute("SELECT 1 FROM od_records WHERE request_id=? AND date=? AND period=? LIMIT 1",(req['id'],date_str,per))
        rec_exists=bool(c.fetchone())
        prev=None
        c.execute('SELECT id FROM attendance_sessions WHERE class_name=? AND date=? AND period=? AND subject_display=?',(req.get('class_name'),date_str,per,disp))
        sr=c.fetchone()
        if sr:
            c.execute('SELECT status,source FROM attendance_entries WHERE session_id=? AND student_id=?',(sr[0],req['student_id']))
            prev=c.fetchone()
        if prev:
            st=(prev[0] or '').strip(); src=(prev[1] or '').strip()
            if st in ('Present','Late'):
                continue
            if st=='OD' and src=='od_workflow':
                if not rec_exists:
                    c.execute("INSERT INTO od_records (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at) VALUES (?,?,?,?,?,?,?,?, 'OD',?,?,?)",
                              (req['id'], req['student_id'], req.get('reg_no') or '', req.get('student_name') or '', req.get('class_name'), date_str, per, 'OD_REQUEST', 'Advisor+HOD Approved', 'od_workflow', now_iso))
                continue
        try:
            sess, _disp = _find_or_create_session(conn, req.get('class_name'), date_str, per)
            elabel=_effective_subject(emaps,req['student_id'],req.get('class_name'),date_str,per,'') if cell.get('options') else ''
            c.execute("INSERT INTO attendance_entries (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at,elective_label) VALUES (?,?,?,?, 'OD',NULL,0,'od_workflow',?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET status='OD',source='od_workflow',marked_by=excluded.marked_by,updated_at=excluded.updated_at,reg_no=excluded.reg_no,student_name=excluded.student_name,elective_label=COALESCE(NULLIF(excluded.elective_label,''),attendance_entries.elective_label)",
                      (sess, req['student_id'], req.get('reg_no') or '', req.get('student_name') or '', 'Advisor+HOD Approved', now_iso, elabel))
            if not rec_exists:
                c.execute("INSERT INTO od_records (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at) VALUES (?,?,?,?,?,?,?,?, 'OD',?,?,?)",
                          (req['id'], req['student_id'], req.get('reg_no') or '', req.get('student_name') or '', req.get('class_name'), date_str, per, 'OD_REQUEST', 'Advisor+HOD Approved', 'od_workflow', now_iso))
            marked += 1
        except Exception:
            pass
    return marked

def _apply_due_od_for_request(conn, req, now=None):
    try:
        d1=datetime.date.fromisoformat((req.get('from_date') or '').strip())
        d2=datetime.date.fromisoformat((req.get('to_date') or req.get('from_date') or '').strip())
    except Exception:
        return 0
    marked=0; cur=d1
    while cur<=d2:
        if not _od_day_due(cur.isoformat(), now): break
        marked += _apply_od_for_date(conn, req, cur.isoformat(), now)
        cur += datetime.timedelta(days=1)
    return marked

def _apply_approved_od(conn, req):
    # HOD approval applies only dates that are already due; future dates wait for the 06:00 IST worker pass.
    return _apply_due_od_for_request(conn, req)

def _apply_due_approved_od(conn=None):
    own = conn is None
    if own: conn = _db()
    total=0; now=_now_ist()
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json']
    try:
        c=conn.cursor()
        c.execute("SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json FROM od_requests WHERE status='od_approved'")
        for row in c.fetchall():
            req=dict(zip(keys,row))
            try:
                total += _apply_due_od_for_request(conn, req, now)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if own:
            conn.commit(); conn.close()
    return total

@app.route('/api/od/review', methods=['POST'])
@staff_required
def api_od_review():
    _expire_od_requests_if_needed()
    if session.get('role')=='admin':
        return jsonify({'ok':False,'error':'Admin is not involved in OD approval/rejection.'}), 403
    data=request.get_json(silent=True) or {}
    rid=data.get('request_id'); action=(data.get('action') or '').strip().lower(); note=(data.get('note') or '').strip()
    if action not in ('advisor_approve','advisor_reject','hod_approve','hod_reject','hod_cancel'): return jsonify({'ok':False,'error':'Invalid action'}), 400
    conn=_db(); c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
    c.execute('SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,proof_path,advisor_code FROM od_requests WHERE id=?',(rid,))
    row=c.fetchone()
    if not row: conn.close(); return jsonify({'ok':False,'error':'Request not found'}), 404
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json','reason','status','proof_path','advisor_code']
    req=dict(zip(keys,row))
    is_hod=bool(session.get('is_hod')); is_adv=bool(session.get('is_advisor'))
    st=req['status']; req_class=req['class_name']; adv_class=session.get('advisor_class')
    advisor_ok = is_adv and (adv_class==req_class)
    if action=='advisor_approve':
        if not advisor_ok: conn.close(); return jsonify({'ok':False,'error':'Only the correct class advisor can approve this request.'}), 403
        if st!='pending_advisor': conn.close(); return jsonify({'ok':False,'error':f'Cannot advisor-approve from status {st}'}), 400
        c.execute('UPDATE od_requests SET status="advisor_approved", advisor_code=?,advisor_decision="approved",advisor_note=?,advisor_at=?,updated_at=? WHERE id=?',
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='advisor_reject':
        if not advisor_ok: conn.close(); return jsonify({'ok':False,'error':'Only the correct class advisor can reject this request.'}), 403
        if st!='pending_advisor': conn.close(); return jsonify({'ok':False,'error':f'Cannot advisor-reject from status {st}'}), 400
        c.execute('UPDATE od_requests SET status="cancelled", advisor_code=?,advisor_decision="rejected",advisor_note=?,advisor_at=?,hod_decision="NIL",hod_note=NULL,hod_at=NULL,updated_at=? WHERE id=?',
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='hod_approve':
        if not is_hod: conn.close(); return jsonify({'ok':False,'error':'Only HOD can give final OD approval.'}), 403
        c.execute('SELECT status FROM od_requests WHERE id=?',(rid,)); st=c.fetchone()[0]
        if st!='advisor_approved': conn.close(); return jsonify({'ok':False,'error':'HOD can approve only after class advisor approval. Current: '+st}), 400
        req['status']='advisor_approved'
        marked=_apply_approved_od(conn, req)
        c.execute('UPDATE od_requests SET status="od_approved", hod_code=?,hod_decision="approved",hod_note=?,hod_at=?,updated_at=? WHERE id=?',
                  (session.get('staff_code') or session.get('username'), (note + f" | auto-applied {marked} due period-entries").strip(' |'), now, now, rid))
        conn.commit(); conn.close()
        if req.get('student_id'):
            _enqueue_alert(req['student_id'], 'od_approved', 'OD approved',
                           f"Your OD request for {req.get('class_name')} ({req.get('from_date')} to {req.get('to_date')}) was approved by HOD. {marked} period entries updated.",
                           event_key='od_approved', source_ref=f'od:{rid}')
        return jsonify({'ok':True,'request_id':rid,'action':action,'status':'od_approved','marked_periods':marked,
                        'message':f'OD approved. Applied {marked} due period-entries; future dates auto-apply date-by-date at/after 06:00 IST.'})
    elif action=='hod_reject':
        if not is_hod: conn.close(); return jsonify({'ok':False,'error':'Only HOD can reject at HOD stage.'}), 403
        c.execute('SELECT status FROM od_requests WHERE id=?',(rid,)); st=c.fetchone()[0]
        if st!='advisor_approved': conn.close(); return jsonify({'ok':False,'error':'HOD can reject only requests pending HOD (advisor approved). Current: '+st}), 400
        c.execute('UPDATE od_requests SET status="cancelled", hod_code=?,hod_decision="rejected",hod_note=?,hod_at=?,updated_at=? WHERE id=?',
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action=='hod_cancel':
        if not is_hod: conn.close(); return jsonify({'ok':False,'error':'Only HOD can cancel approved OD.'}), 403
        c.execute('SELECT status FROM od_requests WHERE id=?',(rid,)); st=c.fetchone()[0]
        if st!='od_approved': conn.close(); return jsonify({'ok':False,'error':'HOD can cancel only approved OD requests.'}), 400
        if not _hod_cancel_od_allowed(req.get('to_date')): conn.close(); return jsonify({'ok':False,'error':'OD period is over; HOD can cancel only until OD gets over.'}), 400
        reverted=_revert_cancelled_od(conn, req)
        c.execute('UPDATE od_requests SET status="cancelled", hod_code=?,hod_decision="cancelled",hod_note=?,hod_at=?,updated_at=? WHERE id=?',
                  (session.get('staff_code') or session.get('username'), (note or 'OD cancelled by HOD') + f' | reverted {reverted} entries to default/absent (kept explicit non-OD Present)', now, now, rid))
        conn.commit(); conn.close()
        if req.get('student_id'):
            _enqueue_alert(req['student_id'], 'od_cancelled', 'OD cancelled',
                           f"Your approved OD for {req.get('class_name')} ({req.get('from_date')} to {req.get('to_date')}) was cancelled by HOD. {reverted} attendance entries were reverted.",
                           event_key='od_cancelled', source_ref=f'od:{rid}')
        return jsonify({'ok':True,'request_id':rid,'action':action,'status':'cancelled','reverted':reverted,'message':'OD cancelled by HOD. OD effect removed; attendance restored to default (Absent) except explicit Present marks by camera/staff.'})
    conn.commit(); conn.close()
    if req.get('student_id'):
        if action=='advisor_approve':
            _enqueue_alert(req['student_id'], 'od_forwarded', 'OD forwarded to HOD',
                           f"Your OD request for {req.get('class_name')} ({req.get('from_date')} to {req.get('to_date')}) was approved by the class advisor and sent to HOD.",
                           event_key='od_forwarded', source_ref=f'od:{rid}')
        elif action in ('advisor_reject','hod_reject'):
            _enqueue_alert(req['student_id'], 'od_rejected', 'OD request rejected',
                           f"Your OD request for {req.get('class_name')} ({req.get('from_date')} to {req.get('to_date')}) was rejected. {note or ''}".strip(),
                           event_key='od_rejected', source_ref=f'od:{rid}')
    return jsonify({'ok':True,'request_id':rid,'action':action})

@app.route('/api/od/proof/<int:rid>')
@staff_required
def api_od_proof(rid):
    if session.get('role')=='admin': abort(403)
    conn=_db(); c=conn.cursor(); c.execute('SELECT class_name,proof_path,proof_name FROM od_requests WHERE id=?',(rid,)); r=c.fetchone(); conn.close()
    if not r: abort(404)
    if not _can_download_od_proof(r[0]): abort(403)
    if not r[1] or not os.path.exists(r[1]): abort(404)
    return send_file(r[1], as_attachment=True, download_name=r[2] or os.path.basename(r[1]))

# --- ERP_MEDICAL_WORKFLOW ---
MEDICAL_ACTIVE_STATUSES = ('pending_advisor', 'advisor_approved', 'medical_approved')
MEDICAL_CERT_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.webp'}

def _save_medical_certificate(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None, 'Medical certificate is required.'
    original = secure_filename(file_storage.filename)
    ext = os.path.splitext(original)[1].lower()
    if ext not in MEDICAL_CERT_EXTENSIONS:
        return None, None, 'Certificate must be a PDF, JPG, PNG, or WEBP file.'
    save = f"medical_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    path = os.path.join(MEDICAL_CERTS_DIR, save)
    file_storage.save(path)
    return path, original, None

def _medical_scope_label(day_wise, periods):
    if day_wise:
        return 'Full Day (All periods 1-7)'
    return 'Selected Periods (' + ','.join(map(str, periods)) + ')'

def _medical_status_label(status, advisor_note=''):
    return {
        'pending_advisor': 'Pending Class Advisor approval',
        'advisor_approved': 'Advisor approved - Awaiting HOD approval',
        'medical_approved': 'Medical Leave Approved (attendance credited)',
        'cancelled': 'Rejected / Cancelled',
    }.get(status, status or '')

def _medical_hod_display(req):
    if (req.get('advisor_decision') or '').lower() == 'rejected':
        return 'NIL'
    return req.get('hod_decision') or 'NIL'

def _parse_medical_periods(periods_json, day_wise):
    periods = []
    try:
        raw = json.loads(periods_json) if isinstance(periods_json, str) else (periods_json or [])
        if isinstance(raw, list):
            for value in raw:
                try:
                    period = int(value)
                except Exception:
                    continue
                if 1 <= period <= 7 and period not in periods:
                    periods.append(period)
    except Exception:
        pass
    if not periods:
        periods = list(range(1, 8))
    return sorted(periods)

def _medical_day_due(date_str, now=None):
    try:
        day = datetime.date.fromisoformat((date_str or '').strip())
    except Exception:
        return False
    now = now or _now_ist()
    return day <= now.date()

def _medical_certificate_allowed(req):
    if session.get('role') == 'student':
        student = _resolve_student_for_user()
        return bool(student and int(student[0]) == int(req.get('student_id') or 0))
    if session.get('role') == 'admin':
        return True
    if session.get('is_hod'):
        return True
    return bool(session.get('is_advisor') and session.get('advisor_class') == req.get('class_name'))

def _apply_medical_for_date(conn, req, date_str, now=None):
    if not _medical_day_due(date_str, now):
        return 0
    if not _od_day_is_working(req.get('class_name'), date_str):
        return 0
    c = conn.cursor(); now_iso = datetime.datetime.utcnow().isoformat()
    periods = _parse_medical_periods(req.get('periods_json'), req.get('day_wise'))
    marked = 0; emaps = _elective_lookup()
    for period in periods:
        cell = timetable_cell(req.get('class_name'), date_str, period)
        if cell:
            disp = (cell.get('display') or cell.get('subject') or f'P{period}').strip()
            sess, disp = _find_or_create_session(conn, req.get('class_name'), date_str, period)
        else:
            c.execute('SELECT id,subject_display FROM attendance_sessions WHERE class_name=? AND date=? AND period=? ORDER BY id LIMIT 1',
                      (req.get('class_name'), date_str, period))
            existing_session = c.fetchone()
            if not existing_session:
                continue
            sess, disp = existing_session[0], (existing_session[1] or f'P{period}').strip()
        c.execute('SELECT 1 FROM medical_records WHERE request_id=? AND date=? AND period=? LIMIT 1',
                  (req['id'], date_str, period))
        rec_exists = bool(c.fetchone())
        c.execute('SELECT status,source FROM attendance_entries WHERE session_id=? AND student_id=?',
                  (sess, req['student_id']))
        prev = c.fetchone()
        if prev:
            status, source = (prev[0] or '').strip(), (prev[1] or '').strip()
            if status in ('Present', 'Late', 'OD'):
                continue
        try:
            elective_label = _effective_subject(emaps, req['student_id'], req.get('class_name'), date_str, period, '') if cell and cell.get('options') else ''
            c.execute("""INSERT INTO attendance_entries
                         (session_id,student_id,reg_no,student_name,status,confidence,flagged,source,marked_by,updated_at,elective_label)
                         VALUES (?,?,?,?, 'Medical Leave',NULL,0,'medical_workflow',?,?,?)
                         ON CONFLICT(session_id,student_id) DO UPDATE SET
                           status='Medical Leave', source='medical_workflow', marked_by=excluded.marked_by,
                           updated_at=excluded.updated_at, reg_no=excluded.reg_no, student_name=excluded.student_name,
                           elective_label=COALESCE(NULLIF(excluded.elective_label,''),attendance_entries.elective_label)""",
                      (sess, req['student_id'], req.get('reg_no') or '', req.get('student_name') or '',
                       'Medical Leave (Advisor+HOD)', now_iso, elective_label))
            if not rec_exists:
                c.execute("""INSERT INTO medical_records
                             (request_id,student_id,reg_no,student_name,class_name,date,period,subject,status,issued_by,issued_role,created_at)
                             VALUES (?,?,?,?,?,?,?,?, 'Medical Leave',?,?,?)""",
                          (req['id'], req['student_id'], req.get('reg_no') or '', req.get('student_name') or '',
                           req.get('class_name'), date_str, period, disp, 'Advisor+HOD Approved', 'medical_workflow', now_iso))
            marked += 1
        except Exception:
            pass
    return marked

def _apply_due_medical_for_request(conn, req, now=None):
    try:
        d1 = datetime.date.fromisoformat((req.get('from_date') or '').strip())
        d2 = datetime.date.fromisoformat((req.get('to_date') or req.get('from_date') or '').strip())
    except Exception:
        return 0
    marked = 0; cur = d1
    while cur <= d2:
        if not _medical_day_due(cur.isoformat(), now):
            break
        marked += _apply_medical_for_date(conn, req, cur.isoformat(), now)
        cur += datetime.timedelta(days=1)
    return marked

def _apply_due_approved_medical(conn=None):
    own = conn is None
    if own:
        conn = _db()
    total = 0; now = _now_ist()
    keys = ['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json']
    try:
        c = conn.cursor()
        c.execute("SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json FROM medical_requests WHERE status='medical_approved'")
        for row in c.fetchall():
            try:
                total += _apply_due_medical_for_request(conn, dict(zip(keys, row)), now)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if own:
            conn.commit(); conn.close()
    return total

def _revert_cancelled_medical(conn, req):
    c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    try:
        c.execute('DELETE FROM medical_records WHERE request_id=?', (req['id'],))
    except Exception:
        pass
    try:
        d1 = datetime.date.fromisoformat((req.get('from_date') or '').strip())
        d2 = datetime.date.fromisoformat((req.get('to_date') or req.get('from_date') or '').strip())
    except Exception:
        return 0
    periods = _parse_medical_periods(req.get('periods_json'), req.get('day_wise'))
    reverted = 0; cur = d1
    while cur <= d2:
        for period in periods:
            try:
                c.execute("""SELECT e.id,e.status,e.source FROM attendance_entries e
                             JOIN attendance_sessions s ON s.id=e.session_id
                             WHERE e.student_id=? AND s.class_name=? AND s.date=? AND s.period=?""",
                          (req['student_id'], req.get('class_name'), cur.isoformat(), period))
                row = c.fetchone()
                if not row:
                    continue
                entry_id, status, source = row
                if (status or '').strip() == 'Medical Leave' and (source or '').strip() == 'medical_workflow':
                    c.execute("UPDATE attendance_entries SET status=?,source='medical_cancel_revert',marked_by=?,updated_at=? WHERE id=?",
                              (UNMARKED_STATUS, 'Medical leave cancelled', now, entry_id))
                    reverted += 1
            except Exception:
                pass
        cur += datetime.timedelta(days=1)
    return reverted

def _advisor_contact_for_class(class_name):
    code, name = _advisor_for_class(class_name)
    contact = {'code': code or '', 'name': name or '', 'phone': '', 'email': ''}
    if not code:
        return contact
    conn = _db(); c = conn.cursor()
    try:
        c.execute('SELECT display_name,phone,email FROM users WHERE upper(COALESCE(staff_code,\'\'))=upper(?) OR upper(username)=upper(?) ORDER BY id LIMIT 1', (code, code))
        row = c.fetchone()
        if row:
            contact['name'] = row[0] or contact['name']
            contact['phone'] = row[1] or ''
            contact['email'] = row[2] or ''
    except Exception:
        pass
    conn.close()
    return contact

@app.route('/medical')
@login_required
def medical_page():
    role = session.get('role')
    student = _resolve_student_for_user() if role == 'student' else None
    contact = _advisor_contact_for_class((student[3] if student else session.get('advisor_class')) or '')
    sem=None
    if student:
        info=parse_cohort_class(student[3] or '')
        by=info.get('batch_start_year') if info else None
        if by is not None:
            sem=_batch_semester_row(by, current_sem_for_batch(by, on_date=_today_ist_str()))
    if not sem: sem=_current_semester()
    return render_template('medical.html', user=_current_user(), student=student, classes=get_classes(),
                           today=_today_ist_str(), advisor_contact=contact,
                           current_semester=_semester_json(sem, batch_start_year=(sem or {}).get('batch_start_year'), on_date=_today_ist_str()) if sem else None)

@app.route('/api/medical/request', methods=['POST'])
@student_required
def api_medical_request():
    student = _resolve_student_for_user()
    if not student:
        return jsonify({'ok': False, 'error': 'Student profile not linked'}), 404
    reason = (request.form.get('reason') or '').strip()
    from_date = (request.form.get('from_date') or '').strip()
    to_date = (request.form.get('to_date') or from_date).strip()
    scope_type = (request.form.get('scope_type') or 'full_day').strip()
    periods_json = (request.form.get('periods_json') or '').strip()
    if not reason or not from_date:
        return jsonify({'ok': False, 'error': 'Reason and date are required'}), 400
    try:
        d1 = datetime.date.fromisoformat(from_date); d2 = datetime.date.fromisoformat(to_date)
        if d2 < d1:
            d2 = d1
        from_date, to_date = d1.isoformat(), d2.isoformat()
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid date'}), 400
    sem, _end = _semester_end_for_date(from_date)
    if not sem:
        return jsonify({'ok': False, 'error': 'No semester is configured for this date.'}), 400
    sem2, _end2 = _semester_end_for_date(to_date)
    if sem2 and int(sem2['id']) != int(sem['id']):
        return jsonify({'ok': False, 'error': 'Medical leave dates must stay within one semester.'}), 400
    day_wise = 1
    periods = list(range(1, 8))
    if scope_type == 'specific' or periods_json:
        day_wise = 0
        periods = _parse_medical_periods(periods_json, 0)
        if periods_json:
            try:
                supplied = [int(x) for x in re.split(r'[\s,]+', periods_json) if x]
                periods = sorted({x for x in supplied if 1 <= x <= 7})
            except Exception:
                periods = []
        if not periods:
            return jsonify({'ok': False, 'error': 'Select at least one period, or choose Full Day.'}), 400
    class_name = (student[3] or '').strip()
    advisor_code, _advisor_name = _advisor_for_class(class_name)
    if not advisor_code:
        return jsonify({'ok': False, 'error': f'Class advisor not found for class "{class_name}".'}), 400
    conn = _db(); c = conn.cursor()
    c.execute("SELECT id FROM medical_requests WHERE student_id=? AND semester_id=? AND status IN (%s) LIMIT 1" % ','.join('?' for _ in MEDICAL_ACTIVE_STATUSES),
              (student[0], sem['id']) + MEDICAL_ACTIVE_STATUSES)
    if c.fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'You already have an active medical leave request for this semester. Wait for its decision or cancel it before resubmitting.'}), 409
    cert_path, cert_name, cert_error = _save_medical_certificate(request.files.get('certificate'))
    if cert_error:
        conn.close()
        return jsonify({'ok': False, 'error': cert_error}), 400
    now = datetime.datetime.utcnow().isoformat()
    scope_label = _medical_scope_label(day_wise, periods)
    c.execute("""INSERT INTO medical_requests
                 (student_id,reg_no,student_name,class_name,semester_id,from_date,to_date,periods_json,day_wise,
                  reason,certificate_path,certificate_name,status,advisor_code,created_at,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending_advisor', ?,?,?)""",
              (student[0], student[5] or '', student[1] or '', class_name, sem['id'], from_date, to_date,
               json.dumps(periods), day_wise, reason + f" [Scope: {scope_label}]", cert_path, cert_name, advisor_code, now, now))
    rid = c.lastrowid; conn.commit(); conn.close()
    audit_log('medical_request', rid, 'submit', None,
              {'class': class_name, 'semester_id': sem['id'], 'from': from_date, 'to': to_date, 'scope': scope_label, 'periods': periods},
              details=reason)
    return jsonify({'ok': True, 'request_id': rid, 'status': 'pending_advisor', 'periods': periods,
                    'message': f'Medical leave request sent to class advisor ({advisor_code}). After advisor approval, HOD must give final approval. Certificate is mandatory.'})

@app.route('/api/medical/my')
@student_required
def api_medical_my():
    student = _resolve_student_for_user()
    if not student:
        return jsonify({'ok': True, 'requests': []})
    conn = _db(); c = conn.cursor()
    c.execute("""SELECT id,from_date,to_date,day_wise,periods_json,reason,status,advisor_decision,advisor_note,advisor_at,
                        hod_decision,hod_note,hod_at,certificate_name,created_at,class_name,advisor_code,semester_id
                 FROM medical_requests WHERE student_id=? ORDER BY id DESC LIMIT 200""", (student[0],))
    rows = c.fetchall(); conn.close()
    keys = ['id','from_date','to_date','day_wise','periods_json','reason','status','advisor_decision','advisor_note','advisor_at',
            'hod_decision','hod_note','hod_at','certificate_name','created_at','class_name','advisor_code','semester_id']
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item['hod_decision_display'] = _medical_hod_display(item)
        item['status_label'] = _medical_status_label(item.get('status'))
        out.append(item)
    return jsonify({'ok': True, 'requests': out})

@app.route('/api/medical/cancel', methods=['POST'])
@student_required
def api_medical_cancel():
    data = request.get_json(silent=True) or request.form
    rid = data.get('request_id')
    student = _resolve_student_for_user()
    if not student:
        return jsonify({'ok': False, 'error': 'Student profile not linked'}), 404
    conn = _db(); c = conn.cursor()
    c.execute("SELECT id,status FROM medical_requests WHERE id=? AND student_id=?", (rid, student[0]))
    row = c.fetchone()
    if not row:
        conn.close(); return jsonify({'ok': False, 'error': 'Request not found'}), 404
    if row[1] not in ('pending_advisor', 'advisor_approved'):
        conn.close(); return jsonify({'ok': False, 'error': 'Only pending requests can be cancelled. Contact HOD for an approved request.'}), 400
    now = datetime.datetime.utcnow().isoformat()
    c.execute("UPDATE medical_requests SET status='cancelled',updated_at=? WHERE id=?", (now, rid))
    conn.commit(); conn.close()
    audit_log('medical_request', rid, 'cancel', None, None, details='Cancelled by student')
    return jsonify({'ok': True, 'request_id': rid, 'message': 'Medical leave request cancelled. You may submit a new request for this semester.'})

@app.route('/api/medical/pending')
@staff_required
def api_medical_pending():
    role = session.get('role'); is_hod = bool(session.get('is_hod')); is_adv = bool(session.get('is_advisor')); adv_class = session.get('advisor_class')
    qbase = """SELECT id,student_id,reg_no,student_name,class_name,semester_id,from_date,to_date,day_wise,periods_json,reason,status,
                      certificate_name,advisor_decision,advisor_note,hod_decision,hod_note,created_at,advisor_code FROM medical_requests """
    conn = _db(); c = conn.cursor()
    if role == 'admin':
        conn.close()
        return jsonify({'ok': True, 'requests': [], 'viewer_role': 'admin', 'note': 'Admin is not involved in medical leave approval. Class advisor and HOD handle requests.'})
    if is_hod and not is_adv:
        viewer_role = 'hod'
        c.execute(qbase + "WHERE status IN ('advisor_approved','medical_approved') ORDER BY (status='advisor_approved') DESC,id DESC LIMIT 300")
    elif is_hod and is_adv:
        viewer_role = 'hod_advisor'
        c.execute(qbase + "WHERE status IN ('advisor_approved','medical_approved') OR (status='pending_advisor' AND class_name=?) ORDER BY id DESC LIMIT 300", (adv_class or '',))
    elif is_adv:
        viewer_role = 'advisor'
        c.execute(qbase + "WHERE class_name=? AND status='pending_advisor' ORDER BY id DESC LIMIT 300", (adv_class,))
    else:
        conn.close()
        return jsonify({'ok': True, 'requests': [], 'viewer_role': 'staff', 'note': 'Only the class advisor and HOD can review medical leave requests.'})
    rows = c.fetchall(); conn.close()
    role_map = _student_roles_map([r[1] for r in rows])
    keys = ['id','student_id','reg_no','student_name','class_name','semester_id','from_date','to_date','day_wise','periods_json','reason','status',
            'certificate_name','advisor_decision','advisor_note','hod_decision','hod_note','created_at','advisor_code']
    reqs = []
    for row in rows:
        item = dict(zip(keys, row))
        item['student_role'] = role_map.get(int(row[1] or 0), '')
        actions = []
        if viewer_role == 'advisor' and item['status'] == 'pending_advisor' and item['class_name'] == adv_class:
            actions = ['advisor_approve', 'advisor_reject']
        elif viewer_role == 'hod':
            if item['status'] == 'advisor_approved':
                actions = ['hod_approve', 'hod_reject']
            elif item['status'] == 'medical_approved' and _hod_cancel_od_allowed(item.get('to_date')):
                actions = ['hod_cancel']
        elif viewer_role == 'hod_advisor':
            if item['status'] == 'advisor_approved':
                actions = ['hod_approve', 'hod_reject']
            elif item['status'] == 'medical_approved' and _hod_cancel_od_allowed(item.get('to_date')):
                actions = ['hod_cancel']
            elif item['status'] == 'pending_advisor' and item['class_name'] == adv_class:
                actions = ['advisor_approve', 'advisor_reject']
        item['allowed_actions'] = actions
        item['hod_decision_display'] = _medical_hod_display(item)
        item['status_label'] = _medical_status_label(item.get('status'))
        reqs.append(item)
    return jsonify({'ok': True, 'requests': reqs, 'viewer_role': viewer_role, 'advisor_class': adv_class})

@app.route('/api/medical/review', methods=['POST'])
@staff_required
def api_medical_review():
    if session.get('role') == 'admin':
        return jsonify({'ok': False, 'error': 'Admin is not involved in medical leave approval/rejection.'}), 403
    data = request.get_json(silent=True) or {}
    rid = data.get('request_id'); action = (data.get('action') or '').strip().lower(); note = (data.get('note') or '').strip()
    if action not in ('advisor_approve', 'advisor_reject', 'hod_approve', 'hod_reject', 'hod_cancel'):
        return jsonify({'ok': False, 'error': 'Invalid action'}), 400
    conn = _db(); c = conn.cursor(); now = datetime.datetime.utcnow().isoformat()
    c.execute("""SELECT id,student_id,reg_no,student_name,class_name,semester_id,from_date,to_date,day_wise,periods_json,reason,status,
                        certificate_path,advisor_code FROM medical_requests WHERE id=?""", (rid,))
    row = c.fetchone()
    if not row:
        conn.close(); return jsonify({'ok': False, 'error': 'Request not found'}), 404
    keys = ['id','student_id','reg_no','student_name','class_name','semester_id','from_date','to_date','day_wise','periods_json','reason','status',
            'certificate_path','advisor_code']
    req = dict(zip(keys, row))
    is_hod = bool(session.get('is_hod')); is_adv = bool(session.get('is_advisor'))
    status = req['status']; advisor_ok = is_adv and session.get('advisor_class') == req['class_name']
    if action == 'advisor_approve':
        if not advisor_ok:
            conn.close(); return jsonify({'ok': False, 'error': 'Only the correct class advisor can approve this request.'}), 403
        if status != 'pending_advisor':
            conn.close(); return jsonify({'ok': False, 'error': f'Cannot advisor-approve from status {status}'}), 400
        c.execute("UPDATE medical_requests SET status='advisor_approved',advisor_code=?,advisor_decision='approved',advisor_note=?,advisor_at=?,updated_at=? WHERE id=?",
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action == 'advisor_reject':
        if not advisor_ok:
            conn.close(); return jsonify({'ok': False, 'error': 'Only the correct class advisor can reject this request.'}), 403
        if status != 'pending_advisor':
            conn.close(); return jsonify({'ok': False, 'error': f'Cannot advisor-reject from status {status}'}), 400
        c.execute("UPDATE medical_requests SET status='cancelled',advisor_code=?,advisor_decision='rejected',advisor_note=?,advisor_at=?,hod_decision='NIL',hod_note=NULL,hod_at=NULL,updated_at=? WHERE id=?",
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action == 'hod_approve':
        if not is_hod:
            conn.close(); return jsonify({'ok': False, 'error': 'Only HOD can give final medical leave approval.'}), 403
        if status != 'advisor_approved':
            conn.close(); return jsonify({'ok': False, 'error': 'HOD can approve only after class advisor approval. Current: ' + status}), 400
        marked = _apply_due_medical_for_request(conn, req)
        c.execute("UPDATE medical_requests SET status='medical_approved',hod_code=?,hod_decision='approved',hod_note=?,hod_at=?,updated_at=? WHERE id=?",
                  (session.get('staff_code') or session.get('username'), (note + f" | auto-applied {marked} due period-entries").strip(' |'), now, now, rid))
        conn.commit(); conn.close()
        audit_log('medical_request', rid, 'approve', None, {'marked_periods': marked}, details=note)
        return jsonify({'ok': True, 'request_id': rid, 'action': action, 'status': 'medical_approved', 'marked_periods': marked,
                        'message': f'Medical leave approved. Applied {marked} due period-entries; future dates auto-apply when the date arrives.'})
    elif action == 'hod_reject':
        if not is_hod:
            conn.close(); return jsonify({'ok': False, 'error': 'Only HOD can reject at HOD stage.'}), 403
        if status != 'advisor_approved':
            conn.close(); return jsonify({'ok': False, 'error': 'HOD can reject only advisor-approved requests. Current: ' + status}), 400
        c.execute("UPDATE medical_requests SET status='cancelled',hod_code=?,hod_decision='rejected',hod_note=?,hod_at=?,updated_at=? WHERE id=?",
                  (session.get('staff_code') or session.get('username'), note, now, now, rid))
    elif action == 'hod_cancel':
        if not is_hod:
            conn.close(); return jsonify({'ok': False, 'error': 'Only HOD can cancel approved medical leave.'}), 403
        if status != 'medical_approved':
            conn.close(); return jsonify({'ok': False, 'error': 'HOD can cancel only approved medical leave.'}), 400
        if not _hod_cancel_od_allowed(req.get('to_date')):
            conn.close(); return jsonify({'ok': False, 'error': 'Medical leave period is over; it can no longer be cancelled.'}), 400
        reverted = _revert_cancelled_medical(conn, req)
        c.execute("UPDATE medical_requests SET status='cancelled',hod_code=?,hod_decision='cancelled',hod_note=?,hod_at=?,updated_at=? WHERE id=?",
                  (session.get('staff_code') or session.get('username'), (note or 'Medical leave cancelled by HOD') + f' | reverted {reverted} credited entries', now, now, rid))
        conn.commit(); conn.close()
        audit_log('medical_request', rid, 'cancel', None, {'reverted': reverted}, details=note)
        return jsonify({'ok': True, 'request_id': rid, 'action': action, 'status': 'cancelled', 'reverted': reverted,
                        'message': f'Medical leave cancelled. {reverted} credited entries were reverted to Absent; explicit Present/Late/OD were kept.'})
    conn.commit(); conn.close()
    audit_log('medical_request', rid, action, None, None, details=note)
    return jsonify({'ok': True, 'request_id': rid, 'action': action})

@app.route('/api/medical/certificate/<int:rid>')
@login_required
def api_medical_certificate(rid):
    conn = _db(); c = conn.cursor()
    c.execute("SELECT student_id,class_name,certificate_path,certificate_name FROM medical_requests WHERE id=?", (rid,))
    row = c.fetchone(); conn.close()
    if not row:
        abort(404)
    req = {'student_id': row[0], 'class_name': row[1]}
    if not _medical_certificate_allowed(req):
        abort(403)
    path = row[2]; name = row[3] or (os.path.basename(path) if path else 'certificate')
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)

@app.route('/api/medical/history')
@staff_required
def api_medical_history():
    role = session.get('role'); is_hod = bool(session.get('is_hod')); is_adv = bool(session.get('is_advisor')); adv_class = session.get('advisor_class')
    cls = (request.args.get('class') or '').strip(); status = (request.args.get('status') or '').strip()
    dfrom = (request.args.get('from') or '').strip(); dto = (request.args.get('to') or '').strip(); q = (request.args.get('q') or '').strip()
    where = []; params = []
    if role != 'admin' and not is_hod:
        if not is_adv:
            return jsonify({'ok': True, 'requests': [], 'note': 'Medical history is visible to class advisor, HOD and admin only.'})
        where.append('class_name=?'); params.append(adv_class or '')
    if cls:
        where.append('upper(class_name)=upper(?)'); params.append(cls)
    if status:
        where.append('status=?'); params.append(status)
    if dfrom:
        where.append('COALESCE(to_date,from_date)>=?'); params.append(dfrom)
    if dto:
        where.append('from_date<=?'); params.append(dto)
    if q:
        where.append('(upper(student_name) LIKE ? OR upper(reg_no) LIKE ? OR CAST(id AS TEXT)=?)')
        params += ['%' + q.upper() + '%', '%' + q.upper() + '%', q]
    wh = (' WHERE ' + ' AND '.join(where)) if where else ''
    scope = []; scope_params = []
    if role != 'admin' and not is_hod and is_adv:
        scope.append('class_name=?'); scope_params.append(adv_class or '')
    scope_wh = (' WHERE ' + ' AND '.join(scope)) if scope else ''
    conn = _db(); c = conn.cursor()
    c.execute("""SELECT id,student_id,reg_no,student_name,class_name,semester_id,from_date,to_date,day_wise,periods_json,reason,status,
                        advisor_decision,advisor_note,hod_decision,hod_note,certificate_name,created_at,updated_at
                 FROM medical_requests""" + wh + " ORDER BY id DESC LIMIT 1000", params)
    rows = c.fetchall(); counts = {}
    try:
        c.execute('SELECT status,count(*) FROM medical_requests' + scope_wh + ' GROUP BY status', scope_params)
        counts = {r[0]: r[1] for r in c.fetchall()}
    except Exception:
        pass
    conn.close()
    role_map = _student_roles_map([r[1] for r in rows])
    keys = ['id','student_id','reg_no','student_name','class_name','semester_id','from_date','to_date','day_wise','periods_json','reason','status',
            'advisor_decision','advisor_note','hod_decision','hod_note','certificate_name','created_at','updated_at']
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item['student_role'] = role_map.get(int(row[1] or 0), '')
        item['hod_decision_display'] = _medical_hod_display(item)
        item['status_label'] = _medical_status_label(item.get('status'))
        out.append(item)
    return jsonify({'ok': True, 'requests': out, 'counts': counts,
                    'viewer': {'role': role, 'is_hod': is_hod, 'is_advisor': is_adv, 'advisor_class': adv_class}})

# --- intermediate ERP additions ---
@app.route('/download_period_csv')
@staff_required
def download_period_csv():
    period=request.args.get('period','all'); cls=(request.args.get('class') or '').strip()
    dfrom,dto=_clean_date_pair(request.args.get('from'),request.args.get('to'))
    absence=(request.args.get('absence') or 'any').strip().lower()
    if absence not in ('any','full_day','partial_day','period_wise','none'): absence='any'
    sem=_semester_by_id(request.args.get('semester_id')) if request.args.get('semester_id') else None
    sem_no_raw=request.args.get('sem_no'); by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,'') and by_raw not in (None,''):
        try: sem=_batch_semester_row(int(by_raw),int(sem_no_raw))
        except Exception: sem=None
    if sem and not dfrom and not dto:
        sd1,sd2=_semester_window(sem, cls or None); report_from=sd1.isoformat(); report_to=sd2.isoformat()
    else:
        report_from,report_to=_report_date_range(period,dfrom,dto)
    conn=_db(); c=conn.cursor(); where=[]; params=[]
    if cls: where.append('upper(s.class_name)=upper(?)'); params.append(cls)
    if report_from: where.append('s.date>=?'); params.append(report_from)
    if report_to: where.append('s.date<=?'); params.append(report_to)
    scope_clause,scope_params=_staff_scope_where()
    if scope_clause: where.append(scope_clause); params += scope_params
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    c.execute("SELECT s.date,s.class_name,s.period,s.subject_display,e.reg_no,e.student_name,e.status,e.marked_by,e.updated_at,e.student_id,e.elective_label FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"+wh+" ORDER BY s.date,s.period,s.class_name,e.student_name", params)
    rows=c.fetchall(); conn.close()
    items=[{'date':r[0],'student_id':r[9],'status_key':_analytics_status_key(r[6])} for r in rows]
    full_days,partial_days,absent_days=_report_absence_flags(items)
    rows=[r for r,item in zip(rows,items) if _report_absence_keep(item,absence,full_days,partial_days,absent_days)]
    emaps=_elective_lookup()
    output=io.StringIO(); output.write('Date,Class,Period,Subject,RegNo,Name,Status,MarkedBy,Updated,SelectedElective\n')
    for r in rows:
        elective=(r[10] or '').strip() or _effective_subject(emaps,r[9],r[1],r[0],r[2],'')
        output.write(','.join('"'+str(x or '').replace('"','""')+'"' for x in list(r[:9])+[elective])+'\n')
    mem=io.BytesIO(output.getvalue().encode('utf-8-sig')); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=f'period_attendance_{period}.csv', mimetype='text/csv')

@app.route('/admin/students/<int:sid>/edit', methods=['GET','POST'])
@admin_required
def admin_edit_student(sid):
    conn=_db(); c=conn.cursor()
    for col in ['email','phone','gender','blood_group','guardian_name','guardian_phone','address']: _ensure_column(c,'students',col,'TEXT')
    _ensure_student_year_columns(c)
    _ensure_student_role_column(c)
    c.execute('SELECT id FROM students WHERE id=?',(sid,))
    if not c.fetchone():
        conn.close(); abort(404)
    if request.method=='POST':
        data=request.form
        name=_norm(data.get('name')); roll=_norm(data.get('roll')); cls=_norm(data.get('class')); sec=_norm(data.get('sec') or data.get('section'))
        reg_no=_upper(data.get('reg_no')); dob=_normalize_dob(data.get('dob'))
        email=_norm(data.get('email')); phone=_norm(data.get('phone'))
        role_value = data.get('student_role_custom') if _norm(data.get('student_role')) == 'Other' else data.get('student_role')
        role = _normalize_student_role(role_value)
        derived_batch, derived_study = _class_year_info(cls)
        batch = _normalize_batch_year(data.get('batch_year')) or derived_batch
        study = _normalize_study_year(data.get('study_year')) or derived_study
        if _normalize_batch_year(data.get('batch_year')) is None or _normalize_study_year(data.get('study_year')) is None:
            conn.close(); flash('Batch year must be YYYY-YYYY and study year must be 1-4.','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        if not name: conn.close(); flash('Student name is required.','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        if not reg_no or not dob: conn.close(); flash('Register Number and DOB are required for student login.','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        if cls and cls not in get_classes():
            conn.close(); flash(f'Unknown class: {cls}','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        c.execute('SELECT id,name,class FROM students WHERE upper(trim(COALESCE(reg_no,"")))=? AND id!=?',(reg_no,sid))
        dup=c.fetchone()
        if dup:
            conn.close(); flash(f'Duplicate Reg No: {reg_no} already belongs to {dup[1]} ({dup[2] or "no class"}).','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        c.execute('SELECT id FROM users WHERE student_id=? AND role="student"',(sid,)); u=c.fetchone()
        c.execute('SELECT id,role,student_id FROM users WHERE upper(username)=? AND id!=?',(reg_no,u[0] if u else -1))
        other=c.fetchone()
        if other:
            conn.close(); flash(f'Login ID {reg_no} is already used by another {other[1]} account.','danger'); return redirect(url_for('admin_edit_student',sid=sid))
        c.execute('''UPDATE students SET name=?,roll=?,class=?,section=?,reg_no=?,dob=?,email=?,phone=?,gender=?,blood_group=?,
                            guardian_name=?,guardian_phone=?,address=?,batch_year=?,study_year=?,student_role=? WHERE id=?''',
                  (name,roll,cls,sec,reg_no,dob,email,phone,_norm(data.get('gender')),_norm(data.get('blood_group')),
                   _norm(data.get('guardian_name')),_norm(data.get('guardian_phone')),_norm(data.get('address')),
                   batch,int(study) if study else None,role,sid))
        if u:
            c.execute('UPDATE users SET username=?,display_name=?,dob=?,password=?,email=?,phone=?,active=1 WHERE id=?',
                      (reg_no,name,dob,_hash_pw(dob),email,phone,u[0]))
        else:
            c.execute('INSERT INTO users (username,password,role,student_id,display_name,dob,email,phone,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (reg_no,_hash_pw(dob),'student',sid,name,dob,email,phone,1,datetime.datetime.utcnow().isoformat()))
        conn.commit(); conn.close()
        audit_log('student', sid, 'edit', None, {'name':name,'reg_no':reg_no,'class':cls,'roll':roll,'batch_year':batch,'study_year':study,'student_role':role}, details='Admin edited student')
        flash('Student updated. Student login password was reset to DOB.','success')
        return redirect(url_for('admin_students_page'))
    c.execute('''SELECT id,name,roll,class,section,reg_no,created_at,dob,email,phone,gender,blood_group,guardian_name,
                        guardian_phone,address,active,batch_year,study_year,student_role FROM students WHERE id=?''',(sid,))
    r=c.fetchone(); conn.close()
    if not r: abort(404)
    keys=['id','name','roll','class','section','reg_no','created_at','dob','email','phone','gender','blood_group','guardian_name','guardian_phone','address','active','batch_year','study_year','student_role']
    student=dict(zip(keys,r))
    student['batch_year']=student.get('batch_year') or _class_year_info(student.get('class'))[0]
    student['study_year']=student.get('study_year') or _class_year_info(student.get('class'))[1]
    return render_template('edit_student.html', student=student, classes=get_classes(), role_presets=STUDENT_ROLE_PRESETS)

@app.route('/reports')
@staff_required
def reports_page():
    role=session.get('role') or 'staff'
    cls=(request.args.get('class') or '').strip()
    by_raw=request.args.get('batch_start_year')
    sem_no_raw=request.args.get('sem_no')
    by=None
    if by_raw not in (None,''):
        try: by=int(by_raw)
        except Exception: by=None
    if by is None and cls:
        info=parse_cohort_class(cls)
        if info: by=info.get('batch_start_year')
    today=_today_ist_str()
    sem_opts=[]
    current_semester_id=None
    current_sem_no=None
    if by is not None:
        ensure_batch_semesters(by, actor=session.get('username') or 'system')
        visible, cur_no = visible_sem_nos(role, by, on_date=today)
        sem_rows=_semester_rows(batch_start_year=by, include_legacy=False)
        sem_opts=[_semester_json(s,batch_start_year=by,on_date=today) for s in sem_rows if role=='admin' or int(s.get('sem_no') or 0) in set(visible)]
        current_sem_no=cur_no
        sel_sem=None
        if sem_no_raw not in (None,''):
            try: sel_sem=next((s for s in sem_opts if int(s.get('sem_no') or 0)==int(sem_no_raw)), None)
            except Exception: sel_sem=None
        current_semester_id=(sel_sem or next((s for s in sem_opts if int(s.get('sem_no') or 0)==int(cur_no)), (sem_opts[0] if sem_opts else None)) or {}).get('id')
    else:
        rows=_semester_rows()
        sem_opts=[_semester_json(s,on_date=today) for s in rows]
        cur=_current_semester(); current_semester_id=cur.get('id') if cur else (sem_opts[0]['id'] if sem_opts else None)
    batches=[{'batch_start_year':b,'label':batch_label(b)} for b in BATCH_START_YEARS]
    return render_template('reports.html', user=_current_user(), classes=get_classes(), semesters=sem_opts, current_semester_id=current_semester_id,
                           batches=batches, batch_start_year=by, current_sem_no=current_sem_no, selected_sem_no=(int(sem_no_raw) if str(sem_no_raw or '').isdigit() else current_sem_no))


# --- ERP: OD history, admin settings, analytics ---
@app.route('/api/od/history')
@staff_required
def api_od_history():
    _expire_od_requests_if_needed()
    role=session.get('role'); is_hod=bool(session.get('is_hod')); is_adv=bool(session.get('is_advisor')); adv_class=session.get('advisor_class')
    cls=(request.args.get('class') or '').strip(); status=(request.args.get('status') or '').strip()
    dfrom=(request.args.get('from') or '').strip(); dto=(request.args.get('to') or '').strip(); q=(request.args.get('q') or '').strip()
    where=[]; params=[]
    if role!='admin' and not is_hod:
        if not is_adv:
            return jsonify({'ok':True,'requests':[],'note':'OD history is visible to class advisor, HOD and admin only.'})
        where.append('class_name=?'); params.append(adv_class or '')
    if cls: where.append('upper(class_name)=upper(?)'); params.append(cls)
    if status: where.append('status=?'); params.append(status)
    if dfrom: where.append('COALESCE(to_date,from_date)>=?'); params.append(dfrom)
    if dto: where.append('from_date<=?'); params.append(dto)
    if q: where.append('(upper(student_name) LIKE ? OR upper(reg_no) LIKE ? OR CAST(id AS TEXT)=?)'); params += ['%'+q.upper()+'%','%'+q.upper()+'%', q]
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    scope=[]; scope_params=[]
    if role!='admin' and not is_hod and is_adv:
        scope.append('class_name=?'); scope_params.append(adv_class or '')
    scope_wh=(' WHERE '+' AND '.join(scope)) if scope else ''
    conn=_db(); c=conn.cursor()
    c.execute("SELECT id,student_id,reg_no,student_name,class_name,from_date,to_date,day_wise,periods_json,reason,status,advisor_decision,advisor_note,hod_decision,hod_note,proof_name,created_at,updated_at FROM od_requests"+wh+" ORDER BY id DESC LIMIT 1000", params)
    rows=c.fetchall()
    counts={}
    try:
        c.execute("SELECT status,count(*) FROM od_requests"+scope_wh+" GROUP BY status", scope_params); counts={r[0]:r[1] for r in c.fetchall()}
    except Exception: pass
    conn.close()
    role_map=_student_roles_map([r[1] for r in rows])
    keys=['id','student_id','reg_no','student_name','class_name','from_date','to_date','day_wise','periods_json','reason','status','advisor_decision','advisor_note','hod_decision','hod_note','proof_name','created_at','updated_at']
    out=[]
    for r in rows:
        d=dict(zip(keys,r))
        d['student_role']=role_map.get(int(r[1] or 0),'')
        d['hod_decision_display']=_od_hod_display(d)
        if d['status']=='pending_advisor': d['status_label']='Pending Advisor'
        elif d['status']=='advisor_approved': d['status_label']='Awaiting HOD'
        elif d['status']=='od_approved': d['status_label']='Approved'
        elif d['status']=='cancelled' and 'Auto-rejected' in (d.get('advisor_note') or ''): d['status_label']='Auto-rejected (18:00 IST)'
        elif d['status']=='cancelled': d['status_label']='Rejected/Cancelled'
        else: d['status_label']=d['status']
        out.append(d)
    return jsonify({'ok':True,'requests':out,'counts':counts,'viewer':{'role':role,'is_hod':is_hod,'is_advisor':is_adv,'advisor_class':adv_class}})

@app.route('/api/admin/settings', methods=['GET','POST'])
@admin_required
def api_admin_settings():
    if request.method=='GET':
        keys=list(ERP_DEFAULTS.keys())+['attendance_cutoff_hour']
        vals={k:get_setting(k, ERP_DEFAULTS.get(k)) for k in keys}
        vals['attendance_cutoff_hour']=str(get_setting('attendance_cutoff_hour', ATTENDANCE_CUTOFF_HOUR))
        sem=_current_semester() or {}
        groups=[
            {'title':'Academic & semester','description':'Batch-relative S1-S8 semesters are managed in Semester Management. These legacy settings remain for compatibility only.','fields':['semester_label','semester_start','semester_end']},
            {'title':'Attendance policy','description':'Controls defaulter reporting and the staff marking window.','fields':['defaulter_threshold','attendance_start_hour','attendance_cutoff_hour','attendance_window_days']},
            {'title':'Calendar enforcement','description':'Choose whether calendar holidays block staff submissions.','fields':['enforce_calendar_holidays']},
        ]
        runtime={'today_ist':_today_ist_str(),'semester_label':sem.get('label') or vals.get('semester_label',''),
                 'semester_start':sem.get('start_date') or vals.get('semester_start',''),
                 'semester_end':sem.get('end_date') or vals.get('semester_end',''),
                  'attendance_start_hour':vals['attendance_start_hour'],
                  'attendance_cutoff_hour':vals['attendance_cutoff_hour'],
                  'attendance_window_days':vals['attendance_window_days'],
                  'hod_correction_note':'HOD/Admin may correct until the semester end date; future dates stay closed. Batch-sem dates are managed in Semester Management.'}
        return jsonify({'ok':True,'settings':vals,'groups':groups,'runtime':runtime})
    data=request.get_json(silent=True) or request.form.to_dict()
    who=session.get('display_name') or session.get('username')
    changed={}
    for k in ('semester_label','semester_start','semester_end','enforce_calendar_holidays'):
        if k in data:
            v=(data.get(k) or '').strip(); set_setting(k,v,who); changed[k]=v
    if any(k in changed for k in ('semester_label','semester_start','semester_end')):
        sem=_current_semester()
        label=changed.get('semester_label', (sem or {}).get('label') or 'Semester 1')
        start=changed.get('semester_start', (sem or {}).get('start_date') or '')
        end=changed.get('semester_end', (sem or {}).get('end_date') or '')
        conn=_db(); c=conn.cursor(); now=datetime.datetime.utcnow().isoformat()
        try:
            if sem: c.execute('UPDATE semesters SET label=?,start_date=?,end_date=?,updated_at=? WHERE id=?',(label,start,end,now,sem['id']))
            else: c.execute('INSERT INTO semesters (label,start_date,end_date,is_current,created_by,created_at,updated_at) VALUES (?,?,?,1,?,?,?)',(label,start,end,who,now,now))
            conn.commit()
        finally:
            conn.close()
    if 'defaulter_threshold' in data:
        try: v=str(max(1,min(100,float(data.get('defaulter_threshold')))))
        except Exception: v='75'
        set_setting('defaulter_threshold',v,who); changed['defaulter_threshold']=v
    if 'attendance_cutoff_hour' in data:
        try: v=str(max(0,min(23,int(float(data.get('attendance_cutoff_hour'))))))
        except Exception: v=str(ATTENDANCE_CUTOFF_HOUR)
        set_setting('attendance_cutoff_hour',v,who); changed['attendance_cutoff_hour']=v
    if 'attendance_start_hour' in data:
        try: v=str(max(0,min(23,int(float(data.get('attendance_start_hour'))))))
        except Exception: v='6'
        set_setting('attendance_start_hour',v,who); changed['attendance_start_hour']=v
    if 'attendance_window_days' in data:
        try: v=str(max(1,min(60,int(float(data.get('attendance_window_days'))))))
        except Exception: v='15'
        set_setting('attendance_window_days',v,who); changed['attendance_window_days']=v
    audit_log('app_settings','global','update',None,changed,details='Admin updated ERP settings')
    return jsonify({'ok':True,'settings':changed})

@app.route('/admin/settings')
@admin_required
def admin_settings_page():
    return render_template('admin_settings.html', user=_current_user())

# --- Notifications: admin configuration, outbox, push subscriptions ---
NOTIFY_SETTING_KEYS = (
    'notify_email','notify_sms','notify_push',
    'email_absent','email_od','email_low_attendance',
    'sms_absent','sms_od','sms_low_attendance',
    'push_absent','push_od','push_low_attendance',
    'low_attendance_alerts','low_attendance_cooldown_days'
)

@app.route('/admin/notifications')
@admin_required
def admin_notifications_page():
    return render_template('admin_notifications.html', user=_current_user())

@app.route('/api/admin/notifications')
@admin_required
def api_admin_notifications():
    conn=_db(); conn.row_factory=sqlite3.Row; c=conn.cursor()
    c.execute("SELECT * FROM notification_outbox ORDER BY id DESC LIMIT 200")
    outbox=[dict(r) for r in c.fetchall()]
    counts={}
    try:
        c.execute('SELECT status,count(*) FROM notification_outbox GROUP BY status'); counts={r[0]:r[1] for r in c.fetchall()}
    except Exception: pass
    c.execute("SELECT id,student_id,alert_type,title,body,created_at,read_at FROM student_alerts ORDER BY id DESC LIMIT 50")
    alerts=[dict(r) for r in c.fetchall()]
    c.execute('SELECT count(*) FROM push_subscriptions WHERE active=1'); push_subs=c.fetchone()[0]
    conn.close()
    return jsonify({'ok':True,'config':_notification_config_status(),'settings':{k:get_setting(k,'') for k in NOTIFY_SETTING_KEYS},
                    'outbox':outbox,'counts':counts,'alerts':alerts,'push_subscriptions':push_subs})

@app.route('/api/admin/notifications/settings', methods=['POST'])
@admin_required
def api_admin_notification_settings():
    data=request.get_json(silent=True) or request.form.to_dict()
    who=session.get('display_name') or session.get('username')
    changed={}
    for k in NOTIFY_SETTING_KEYS:
        if k not in data: continue
        v=str(data.get(k) or '').strip()
        if k == 'low_attendance_cooldown_days':
            try: v=str(max(1,min(365,int(v))))
            except Exception: v='7'
        elif v not in ('0','1'):
            v='1' if str(v).lower() in ('true','on','yes') else '0'
        set_setting(k,v,who); changed[k]=v
    audit_log('notifications','global','settings',None,changed,details='Admin updated notification settings')
    return jsonify({'ok':True,'settings':{k:get_setting(k,'') for k in NOTIFY_SETTING_KEYS}})

@app.route('/api/admin/notifications/process', methods=['POST'])
@admin_required
def api_admin_notifications_process():
    result=process_notification_outbox(50)
    audit_log('notifications','outbox','process',None,result,details='Admin manually processed notification outbox')
    return jsonify({'ok':True,**result})

@app.route('/api/admin/notifications/retry/<int:outbox_id>', methods=['POST'])
@admin_required
def api_admin_notifications_retry(outbox_id):
    conn=_db(); c=conn.cursor(); c.execute("UPDATE notification_outbox SET status='pending', attempts=0, last_error=NULL, claimed_at=NULL WHERE id=?",(outbox_id,)); conn.commit(); conn.close()
    result=process_notification_outbox(only_ids=[outbox_id])
    return jsonify({'ok':True,**result})

@app.route('/api/admin/notifications/test', methods=['POST'])
@admin_required
def api_admin_notifications_test():
    data=request.get_json(silent=True) or {}
    channel=(data.get('channel') or 'email').strip(); kind=(data.get('kind') or 'test').strip()
    student_id=data.get('student_id')
    try: student_id=int(student_id)
    except Exception: return jsonify({'ok':False,'error':'student_id required'}), 400
    student=_student_contacts(student_id)
    if not student: return jsonify({'ok':False,'error':'Student not found'}), 404
    title='ERP notification test'; body=f'Test {channel} message for {student["name"]}.'
    try:
        if channel=='email': _send_email_message(student['email'], title, body)
        elif channel=='sms': _send_sms_message(student['phone'] or student['guardian_phone'], title+'\n'+body)
        elif channel=='push': _send_push_message(student_id, title, body)
        else: return jsonify({'ok':False,'error':'Unknown channel'}), 400
        return jsonify({'ok':True,'message':f'{channel} test sent.'})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}), 400

@app.route('/api/push/public_key')
@login_required
def api_push_public_key():
    key=_env('VAPID_PUBLIC_KEY')
    return jsonify({'ok':bool(key),'publicKey':key})

@app.route('/api/push/subscribe', methods=['POST'])
@student_required
def api_push_subscribe():
    student=_resolve_student_for_user()
    if not student: return jsonify({'ok':False,'error':'No linked student profile'}), 404
    data=request.get_json(silent=True) or {}
    sub=data.get('subscription') or data
    endpoint=(sub.get('endpoint') or '').strip(); keys=sub.get('keys') or {}
    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify({'ok':False,'error':'Invalid push subscription'}), 400
    conn=_db(); c=conn.cursor()
    c.execute("""INSERT INTO push_subscriptions(student_id,endpoint,p256dh,auth,user_agent,active,created_at)
                 VALUES (?,?,?,?,?,1,?) ON CONFLICT(endpoint) DO UPDATE SET student_id=excluded.student_id,p256dh=excluded.p256dh,auth=excluded.auth,user_agent=excluded.user_agent,active=1""",
              (student[0],endpoint,keys.get('p256dh'),keys.get('auth'),request.headers.get('User-Agent',''),_now_iso()))
    conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/api/push/unsubscribe', methods=['POST'])
@student_required
def api_push_unsubscribe():
    data=request.get_json(silent=True) or {}
    endpoint=(data.get('endpoint') or '').strip()
    conn=_db(); c=conn.cursor(); c.execute('UPDATE push_subscriptions SET active=0 WHERE endpoint=?',(endpoint,)); conn.commit(); conn.close()
    return jsonify({'ok':True})

@app.route('/sw.js')
def service_worker_file():
    path=os.path.join(APP_DIR,'static','sw.js')
    if not os.path.exists(path): abort(404)
    resp=send_file(path, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed']='/'
    resp.headers['Cache-Control']='no-cache'
    return resp

@app.route('/api/analytics/overview')
@staff_required
def api_analytics_overview():
    cls=(request.args.get('class') or '').strip(); dfrom,dto=_clean_date_pair(request.args.get('from'),request.args.get('to'))
    sem=_semester_by_id(request.args.get('semester_id')) if request.args.get('semester_id') else None
    sem_no_raw=request.args.get('sem_no')
    by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,'') and by_raw not in (None,''):
        try: sem=_batch_semester_row(int(by_raw), int(sem_no_raw))
        except Exception: sem=None
    if sem and not dfrom and not dto:
        sd1,sd2=_semester_window(sem, cls or None)
        dfrom=sd1.isoformat(); dto=sd2.isoformat()
    try: threshold=float(get_setting('defaulter_threshold','75') or 75)
    except Exception: threshold=75.0
    conn=_db(); c=conn.cursor(); where=[]; params=[]
    if cls: where.append('upper(s.class_name)=upper(?)'); params.append(cls)
    if dfrom: where.append('s.date>=?'); params.append(dfrom)
    if dto: where.append('s.date<=?'); params.append(dto)
    if session.get('role')!='admin':
        code=(session.get('staff_code') or session.get('username') or '').strip()
        name=(session.get('display_name') or '').strip()
        where.append("(upper(COALESCE(s.staff_code,''))=upper(?) OR upper(COALESCE(s.staff_name,''))=upper(?))")
        params += [code,name]
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    c.execute("SELECT e.student_id,e.student_name,e.reg_no,s.class_name,e.status,count(*) FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"+wh+" GROUP BY e.student_id,s.class_name,e.status", params)
    agg={}
    for sid,name,reg,cn,st,cnt in c.fetchall():
        stn=_normalize_attendance_status(st)
        if stn==UNMARKED_STATUS: continue
        key=(sid,name,reg,cn); a=agg.setdefault(key,{'present':0,'absent':0,'od':0,'late':0,'medical':0,'total':0})
        a['total']+=cnt
        if stn=='Present': a['present']+=cnt
        elif stn in ('Absent','N'): a['absent']+=cnt
        elif stn=='OD': a['od']+=cnt
        elif stn=='Late': a['late']+=cnt
        elif stn=='Medical Leave': a['medical']+=cnt
    students=[]
    for (sid,name,reg,cn),a in agg.items():
        credited=a['present']+a['od']+a['late']+a['medical']
        pct=round((credited/a['total']*100),2) if a['total'] else 0.0
        students.append({'student_id':sid,'name':name,'reg_no':reg,'class':cn,'present':a['present'],'absent':a['absent'],'od':a['od'],'late':a['late'],'medical':a['medical'],'total':a['total'],'percentage':pct,'defaulter':pct<threshold})
    students.sort(key=lambda x:(x['percentage'], x['name'] or ''))
    defaulters=[s for s in students if s['defaulter']]
    emaps=_elective_lookup()
    c.execute("SELECT s.class_name,s.subject_display,s.date,s.period,e.student_id,e.status,e.elective_label FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"+wh+" ORDER BY s.class_name,s.date,s.period", params)
    sub_agg={}
    for cn,subj,dt,pd,sid,st,entry_label in c.fetchall():
        stn=_normalize_attendance_status(st)
        if stn==UNMARKED_STATUS: continue
        label=(entry_label or '').strip() or _effective_subject(emaps,sid,cn,dt,pd,subj or '')
        key=(cn or '',label or '')
        x=sub_agg.setdefault(key,{'total':0,'credited':0})
        x['total']+=1
        if _is_attendance_credited(stn): x['credited']+=1
    subjects=[{'class':k[0],'subject':k[1], 'total':v['total'],'credited':v['credited'],
               'percentage':round((v['credited']/(v['total'] or 1))*100,2)} for k,v in sorted(sub_agg.items())]
    c.execute("SELECT s.date,s.class_name,count(DISTINCT s.period) FROM attendance_sessions s"+wh+" GROUP BY s.date,s.class_name ORDER BY s.date DESC LIMIT 60", params)
    compliance=[{'date':r[0],'class':r[1],'submitted_periods':r[2]} for r in c.fetchall()]
    conn.close()
    if sem:
        semester={'id':sem.get('id'),'label':sem.get('label') or 'Semester','start':dfrom or (sem.get('start_date') or ''),'end':dto or (sem.get('end_date') or ''),'is_current':int(sem.get('is_current') or 0),
                  'sem_no':sem.get('sem_no'),'batch_start_year':sem.get('batch_start_year')}
    else:
        info=parse_cohort_class(cls) if cls else None
        by=info.get('batch_start_year') if info else None
        if by is not None:
            cur_no=current_sem_for_batch(by)
            sem2=_batch_semester_row(by,cur_no)
            semester=_semester_json(sem2,batch_start_year=by,on_date=_today_ist_str()) if sem2 else {'id':None,'label':f'Sem {cur_no}','start':dfrom or '','end':dto or '','is_current':0}
        else:
            semester={'id':None,'label':get_setting('semester_label','Current Semester'),'start':dfrom or get_setting('semester_start',''),'end':dto or get_setting('semester_end',''),'is_current':0}
    return jsonify({'ok':True,'students':students,'defaulters':defaulters,'subjects':subjects,'compliance':compliance,'threshold':threshold,'semester':semester})

def _analytics_status_key(status):
    normalized=_normalize_attendance_status(status)
    if normalized==UNMARKED_STATUS: return 'unmarked'
    if normalized=='Medical Leave': return 'medical'
    if normalized in ('Present','Absent','Late','OD','N'): return 'absent' if normalized=='N' else normalized.lower()
    return 'other'

def _analytics_blank():
    return {'present':0,'absent':0,'late':0,'od':0,'medical':0,'other':0,'unmarked':0,
            'total':0,'credited':0,'absent_minutes':0}

def _analytics_add(bucket, item):
    key=item['status_key']
    if key=='unmarked':
        bucket['unmarked']+=1
        return
    if key in ('present','absent','late','od','medical'): bucket[key]+=1
    else: bucket['other']+=1
    bucket['total']+=1
    if key in ('present','late','od','medical'): bucket['credited']+=1
    if key=='absent': bucket['absent_minutes']+=int(item.get('duration') or 60)

def _analytics_finalize(bucket):
    total=int(bucket.get('total') or 0); credited=int(bucket.get('credited') or 0)
    out=dict(bucket)
    out['percentage']=round((credited/total*100),2) if total else 0.0
    out['absent_hours']=round((int(bucket.get('absent_minutes') or 0))/60.0,2)
    return out

@app.route('/api/analytics/insights')
@staff_required
def api_analytics_insights():
    role=session.get('role'); is_admin=(role=='admin')
    cls=(request.args.get('class') or '').strip()
    dfrom,dto=_clean_date_pair(request.args.get('from'),request.args.get('to'))
    absence=(request.args.get('absence') or 'any').strip().lower()
    if absence not in ('any','full_day','partial_day','period_wise','none'): absence='any'
    mode=(request.args.get('compare') or 'class').strip().lower()
    if mode not in ('class','staff','period','student'): mode='class'
    if not is_admin and mode not in ('class','student'): mode='class'
    sem=_semester_by_id(request.args.get('semester_id')) if request.args.get('semester_id') else None
    sem_no_raw=request.args.get('sem_no')
    by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,'') and by_raw not in (None,''):
        try: sem=_batch_semester_row(int(by_raw), int(sem_no_raw))
        except Exception: sem=None
    if sem and not dfrom and not dto:
        sd1,sd2=_semester_window(sem, cls or None)
        dfrom=sd1.isoformat(); dto=sd2.isoformat()
    try: threshold=float(get_setting('defaulter_threshold','75') or 75)
    except Exception: threshold=75.0
    conn=_db(); c=conn.cursor(); where=[]; params=[]
    if cls: where.append('upper(s.class_name)=upper(?)'); params.append(cls)
    if dfrom: where.append('s.date>=?'); params.append(dfrom)
    if dto: where.append('s.date<=?'); params.append(dto)
    if not is_admin:
        code=(session.get('staff_code') or session.get('username') or '').strip()
        name=(session.get('display_name') or '').strip()
        where.append("(upper(COALESCE(s.staff_code,''))=upper(?) OR upper(COALESCE(s.staff_name,''))=upper(?))")
        params += [code,name]
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    try:
        c.execute("""SELECT e.id,e.student_id,e.student_name,e.reg_no,s.class_name,s.date,s.period,
                            s.subject_display,e.status,e.elective_label,s.staff_code,s.staff_name,
                            s.duration_minutes,e.source
                     FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"""+wh+
                  " ORDER BY s.date DESC,s.period ASC,e.student_name", params)
        rows=c.fetchall()
    except Exception:
        rows=[]
    conn.close()
    roles=_student_roles_map([r[1] for r in rows])
    items=[]
    for r in rows:
        try: per=int(r[6] or 0)
        except Exception: per=0
        try: dur=int(r[12] or 60)
        except Exception: dur=60
        items.append({'entry_id':r[0],'student_id':r[1],'student_name':r[2] or '','reg_no':r[3] or '',
                      'class_name':r[4] or '','date':r[5] or '','period':per,'subject':r[7] or '',
                       'status':r[8] or '','status_key':_analytics_status_key(r[8]),
                       'student_role':roles.get(int(r[1] or 0),''),
                       'elective_label':r[9] or '','staff_code':(r[10] or '').strip(),
                      'staff_name':(r[11] or '').strip(),'duration':dur,'source':r[13] or ''})
    by_day={}
    for it in items:
        if it['status_key']=='unmarked': continue
        by_day.setdefault((it['student_id'],it['date']),[]).append(it['status_key'])
    full_days=set(); partial_days=set(); absent_days=set()
    for day_key,statuses in by_day.items():
        absent_count=sum(1 for s in statuses if s=='absent')
        if absent_count:
            absent_days.add(day_key)
            if absent_count==len(statuses): full_days.add(day_key)
            else: partial_days.add(day_key)
    def keep(it):
        day_key=(it['student_id'],it['date'])
        if absence=='full_day': return day_key in full_days
        if absence=='partial_day': return day_key in partial_days
        if absence=='period_wise': return it['status_key']=='absent'
        if absence=='none': return day_key not in absent_days
        return True
    filtered=[it for it in items if keep(it)]
    totals=_analytics_blank(); student_buckets={}; class_buckets={}; staff_buckets={}; period_buckets={}
    for it in filtered:
        _analytics_add(totals,it)
        sid=it['student_id']
        sb=student_buckets.setdefault(sid,{'student_id':sid,'name':it['student_name'],'reg_no':it['reg_no'],
                                           'student_role':it.get('student_role',''),'class':it['class_name'],'metrics':_analytics_blank()})
        sb['name']=it['student_name'] or sb['name']; sb['reg_no']=it['reg_no'] or sb['reg_no']; sb['class']=it['class_name'] or sb['class']
        _analytics_add(sb['metrics'],it)
        cn=it['class_name'] or 'Unassigned'
        cb=class_buckets.setdefault(cn,{'key':cn,'label':cn,'sub':'Class','metrics':_analytics_blank()})
        _analytics_add(cb['metrics'],it)
        per=it['period'] or 0
        pb=period_buckets.setdefault(per,{'key':str(per),'label':f'P{per}' if per else 'Unknown','sub':'Period','metrics':_analytics_blank()})
        _analytics_add(pb['metrics'],it)
        code=it['staff_code']; sname=it['staff_name']
        if code.upper()!='SYSTEM' and (code or sname):
            sk=code or sname
            sb2=staff_buckets.setdefault(sk,{'key':sk,'label':sname or code,'sub':code or 'Staff','staff_code':code,
                                             'staff_name':sname,'metrics':_analytics_blank()})
            _analytics_add(sb2['metrics'],it)
    students=[]
    for sb in student_buckets.values():
        m=_analytics_finalize(sb.pop('metrics'))
        sb.update(m); sb['defaulter']=bool(m['total'] and m['percentage']<threshold)
        sid=sb['student_id']
        sb['full_day_absences']=sum(1 for (dsid,_d) in full_days if dsid==sid)
        sb['partial_day_absences']=sum(1 for (dsid,_d) in partial_days if dsid==sid)
        students.append(sb)
    students.sort(key=lambda x:(x['percentage'], x['name'] or ''))
    defaulters=[s for s in students if s.get('defaulter')]
    class_rows=[dict(v, **(_analytics_finalize(v['metrics']))) for v in class_buckets.values()]
    staff_rows=[dict(v, **(_analytics_finalize(v['metrics']))) for v in staff_buckets.values()]
    period_rows=[dict(v, **(_analytics_finalize(v['metrics']))) for v in period_buckets.values()]
    for row in class_rows+staff_rows+period_rows+students:
        row.pop('metrics',None)
    compare_rows=[]
    if mode=='class': compare_rows=class_rows
    elif mode=='staff': compare_rows=staff_rows
    elif mode=='period': compare_rows=period_rows
    else:
        compare_rows=[{'key':str(s['student_id']),'label':(('Rep ' if _is_rep_role(s.get('student_role')) else '')+(s['name'] or '')),'sub':(s['reg_no'] or '')+(' - '+s['class'] if s['class'] else ''),
                       **{k:s[k] for k in ('present','absent','late','od','medical','other','total','credited','absent_minutes','percentage','absent_hours','defaulter')}} for s in students]
    compare_rows.sort(key=lambda x:(x.get('percentage') or 0, x.get('total') or 0, x.get('label') or ''), reverse=True)
    totals=_analytics_finalize(totals)
    if sem:
        semester={'id':sem.get('id'),'label':sem.get('label') or 'Semester','start':dfrom or (sem.get('start_date') or ''),'end':dto or (sem.get('end_date') or ''),'is_current':int(sem.get('is_current') or 0),
                  'sem_no':sem.get('sem_no'),'batch_start_year':sem.get('batch_start_year')}
    else:
        info=parse_cohort_class(cls) if cls else None
        by=info.get('batch_start_year') if info else None
        if by is not None:
            cur_no=current_sem_for_batch(by)
            sem2=_batch_semester_row(by,cur_no)
            semester=_semester_json(sem2,batch_start_year=by,on_date=_today_ist_str()) if sem2 else {'id':None,'label':f'Sem {cur_no}','start':dfrom or '','end':dto or '','is_current':0}
        else:
            semester={'id':None,'label':get_setting('semester_label','Current Semester'),'start':dfrom or get_setting('semester_start',''),'end':dto or get_setting('semester_end',''),'is_current':0}
    return jsonify({'ok':True,'students':students,'defaulters':defaulters,'threshold':threshold,'semester':semester,
                    'range':{'from':dfrom,'to':dto},'scope':'all' if is_admin else 'staff_handled',
                    'filters':{'absence':absence,'compare':mode,'class':cls},
                    'totals':totals,'comparison':{'mode':mode,'rows':compare_rows},
                    'comparisons':{'class':class_rows,'staff':staff_rows,'period':period_rows},
                    'note':('Admin view covers all matching sessions.' if is_admin else 'Staff view covers only sessions handled/submitted by you.')})

@app.route('/admin/audit')
@admin_required
def admin_audit_page():
    return render_template('admin_audit.html', user=_current_user())

@app.route('/api/admin/audit')
@admin_required
def api_admin_audit():
    q=(request.args.get('q') or '').strip(); etype=(request.args.get('entity') or '').strip(); action=(request.args.get('action') or '').strip()
    actor=(request.args.get('actor') or '').strip(); date_from=(request.args.get('from') or '').strip(); date_to=(request.args.get('to') or '').strip()
    try: limit=max(1,min(int(request.args.get('limit') or 200),1000))
    except Exception: limit=200
    try: offset=max(0,int(request.args.get('offset') or 0))
    except Exception: offset=0
    where=[]; params=[]
    if q:
        where.append('(upper(actor_name) LIKE ? OR upper(actor_user) LIKE ? OR upper(entity_type) LIKE ? OR upper(action) LIKE ? OR upper(CAST(entity_id AS TEXT)) LIKE ? OR upper(old_value) LIKE ? OR upper(new_value) LIKE ? OR upper(details) LIKE ?)')
        params += ['%'+q.upper()+'%']*8
    if etype: where.append('entity_type=?'); params.append(etype)
    if action: where.append('action=?'); params.append(action)
    if actor: where.append('(upper(actor_name)=upper(?) OR upper(actor_user)=upper(?))'); params += [actor,actor]
    if date_from: where.append('date(ts)>=?'); params.append(date_from)
    if date_to: where.append('date(ts)<=?'); params.append(date_to)
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    conn=_db(); c=conn.cursor()
    c.execute('SELECT count(*) FROM attendance_audit_log'+wh, params)
    total=c.fetchone()[0]
    c.execute('SELECT id,actor_role,actor_user,actor_name,entity_type,entity_id,action,old_value,new_value,details,ts FROM attendance_audit_log'+wh+' ORDER BY id DESC LIMIT ? OFFSET ?', params+[limit,offset])
    rows=c.fetchall()
    facets={}
    for key,col in (('entities','entity_type'),('actions','action'),('actors','actor_name')):
        try:
            c.execute(f'SELECT DISTINCT {col} FROM attendance_audit_log WHERE {col} IS NOT NULL AND {col}!="" ORDER BY {col} COLLATE NOCASE')
            facets[key]=[r[0] for r in c.fetchall()]
        except Exception: facets[key]=[]
    conn.close()
    keys=['id','actor_role','actor_user','actor_name','entity_type','entity_id','action','old_value','new_value','details','ts']
    return jsonify({'ok':True,'logs':[dict(zip(keys,r)) for r in rows],'total':total,'returned':len(rows),'limit':limit,'offset':offset,'facets':facets})

@app.route('/download_defaulters_csv')
@staff_required
def download_defaulters_csv():
    cls=(request.args.get('class') or '').strip()
    dfrom,dto=_clean_date_pair(request.args.get('from'),request.args.get('to'))
    absence=(request.args.get('absence') or 'any').strip().lower()
    if absence not in ('any','full_day','partial_day','period_wise','none'): absence='any'
    sem=_semester_by_id(request.args.get('semester_id')) if request.args.get('semester_id') else None
    sem_no_raw=request.args.get('sem_no'); by_raw=request.args.get('batch_start_year')
    if not sem and sem_no_raw not in (None,'') and by_raw not in (None,''):
        try: sem=_batch_semester_row(int(by_raw),int(sem_no_raw))
        except Exception: sem=None
    if sem and not dfrom and not dto:
        sd1,sd2=_semester_window(sem, cls or None); report_from=sd1.isoformat(); report_to=sd2.isoformat()
    else:
        report_from,report_to=_report_date_range('all',dfrom,dto)
    try: threshold=float(get_setting('defaulter_threshold','75') or 75)
    except Exception: threshold=75.0
    conn=_db(); c=conn.cursor(); where=[]; params=[]
    if cls: where.append('upper(s.class_name)=upper(?)'); params.append(cls)
    if report_from: where.append('s.date>=?'); params.append(report_from)
    if report_to: where.append('s.date<=?'); params.append(report_to)
    scope_clause,scope_params=_staff_scope_where()
    if scope_clause: where.append(scope_clause); params += scope_params
    wh=(' WHERE '+' AND '.join(where)) if where else ''
    c.execute("SELECT e.student_id,e.student_name,e.reg_no,s.class_name,s.date,e.status FROM attendance_entries e JOIN attendance_sessions s ON s.id=e.session_id"+wh+" ORDER BY e.student_name", params)
    raw=c.fetchall(); conn.close()
    items=[{'student_id':r[0],'date':r[4],'status_key':_analytics_status_key(r[5])} for r in raw]
    full_days,partial_days,absent_days=_report_absence_flags(items)
    agg={}
    for r,item in zip(raw,items):
        if not _report_absence_keep(item,absence,full_days,partial_days,absent_days): continue
        sid,name,reg,cn,_dt,st=r
        stn=_normalize_attendance_status(st)
        if stn==UNMARKED_STATUS: continue
        a=agg.setdefault((sid,name,reg,cn),{'p':0,'a':0,'od':0,'l':0,'t':0}); a['t']+=1
        if stn in ('Present','Medical Leave'): a['p']+=1
        elif stn in ('Absent','N'): a['a']+=1
        elif stn=='OD': a['od']+=1
        elif stn=='Late': a['l']+=1
    emaps=_elective_lookup()
    out=io.StringIO(); out.write('RegNo,Name,Class,Present/ML,Absent/N,OD,Late,Total,Percentage,Defaulter(<%s%%),Electives\n'%threshold)
    for (sid,name,reg,cn),a in sorted(agg.items(), key=lambda kv:(kv[0][3] or '', kv[0][1] or '')):
        pct=round(((a['p']+a['od']+a['l'])/a['t']*100),2) if a['t'] else 0.0
        if pct>=threshold: continue
        emap={}
        if sid is not None:
            for (msid,mcn,_mvid),m in emaps.items():
                if msid==int(sid) and mcn==(cn or '').upper(): emap.update(m)
        electives='; '.join(sorted(set(emap.values())))
        out.write(','.join('"'+str(x or '').replace('"','""')+'"' for x in [reg,name,cn,a['p'],a['a'],a['od'],a['l'],a['t'],pct,'YES',electives])+'\n')
    mem=io.BytesIO(out.getvalue().encode('utf-8-sig')); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='defaulters.csv', mimetype='text/csv')

if callable(globals().get('_face_recover_interrupted_jobs')): _face_recover_interrupted_jobs()
if callable(globals().get('_start_notify_worker')): _start_notify_worker()
if callable(globals().get('_start_absence_worker')): _start_absence_worker()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
