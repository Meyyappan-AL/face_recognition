import os
import json
import threading
import cv2
import numpy as np
import pickle
from sklearn.ensemble import RandomForestClassifier

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = "model.pkl"
FACE_MODEL_META_PATH = os.path.join(_APP_DIR, "model.meta.json")

_haar_cascade = None
_mp_face_detector = None
_mp_detector_kind = None  # 'solutions' | 'tasks' | None
_yunet_detector = None
_yunet_path = None
_yunet_size = None
_sface_recognizer = None
_sface_path = None
_detector_lock = threading.Lock()
_sface_lock = threading.Lock()


def _get_haar():
    global _haar_cascade
    if _haar_cascade is None:
        try:
            path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            _haar_cascade = cv2.CascadeClassifier(path)
        except Exception:
            _haar_cascade = cv2.CascadeClassifier()
    return _haar_cascade


def _detect_face_bbox(bgr_image):
    """Return (x1,y1,x2,y2) for the best face, or None. Tries MediaPipe then Haar."""
    global _mp_face_detector, _mp_detector_kind
    h, w = bgr_image.shape[:2]
    if h < 10 or w < 10:
        return None
    rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)

    # Try MediaPipe solutions API (legacy)
    try:
        import mediapipe as mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection"):
            if _mp_detector_kind != "solutions" or _mp_face_detector is None:
                _mp_face_detector = mp.solutions.face_detection.FaceDetection(
                    model_selection=0, min_detection_confidence=0.5
                )
                _mp_detector_kind = "solutions"
            results = _mp_face_detector.process(rgb)
            if results and results.detections:
                det = results.detections[0]
                bbox = det.location_data.relative_bounding_box
                x1 = int(max(0, bbox.xmin * w))
                y1 = int(max(0, bbox.ymin * h))
                x2 = int(min(w, (bbox.xmin + bbox.width) * w))
                y2 = int(min(h, (bbox.ymin + bbox.height) * h))
                if x2 > x1 and y2 > y1:
                    return (x1, y1, x2, y2)
    except Exception:
        pass

    # Try MediaPipe tasks API if a model asset exists
    try:
        import mediapipe as mp
        if hasattr(mp, "tasks") and (_mp_detector_kind in (None, "tasks") or _mp_face_detector is None):
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            model_candidates = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "blaze_face_short_range.tflite"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "blaze_face_short_range.tflite"),
            ]
            model_path = next((p for p in model_candidates if os.path.exists(p)), None)
            if model_path:
                if _mp_detector_kind != "tasks" or _mp_face_detector is None:
                    base_options = mp_python.BaseOptions(model_asset_path=model_path)
                    options = vision.FaceDetectorOptions(
                        base_options=base_options, min_detection_confidence=0.5
                    )
                    _mp_face_detector = vision.FaceDetector.create_from_options(options)
                    _mp_detector_kind = "tasks"
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = _mp_face_detector.detect(mp_image)
                if result and result.detections:
                    bb = result.detections[0].bounding_box
                    x1 = int(max(0, bb.origin_x))
                    y1 = int(max(0, bb.origin_y))
                    x2 = int(min(w, bb.origin_x + bb.width))
                    y2 = int(min(h, bb.origin_y + bb.height))
                    if x2 > x1 and y2 > y1:
                        return (x1, y1, x2, y2)
    except Exception:
        pass

    # Fallback: OpenCV Haar cascade
    try:
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        faces = _get_haar().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(40, 40)
        )
        if len(faces) > 0:
            # pick largest face
            x, y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
            return (int(x), int(y), int(x + fw), int(y + fh))
    except Exception:
        pass
    return None


def _get_yunet():
    global _yunet_detector, _yunet_path, _yunet_size
    candidates = [
        os.environ.get("FACE_DETECTOR_MODEL", ""),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "face_detection_yunet_2023mar.onnx"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "face_detection_yunet_2023mar.onnx"),
    ]
    model_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not model_path or not hasattr(cv2, "FaceDetectorYN"):
        return None
    if _yunet_detector is None or _yunet_path != model_path:
        try:
            _yunet_detector = cv2.FaceDetectorYN.create(model_path, "", (320, 320), 0.6, 0.3, 5000)
            _yunet_path = model_path
            _yunet_size = (320, 320)
        except Exception:
            _yunet_detector = None
            _yunet_path = None
    return _yunet_detector


def _resolve_model_path():
    return MODEL_PATH if os.path.isabs(MODEL_PATH) else os.path.join(_APP_DIR, MODEL_PATH)


def _model_meta():
    try:
        with open(FACE_MODEL_META_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_sface():
    global _sface_recognizer, _sface_path
    candidates = [
        os.environ.get("ERP_FACE_RECOGNIZER_MODEL", ""),
        os.environ.get("FACE_RECOGNIZER_MODEL", ""),
        os.path.join(_APP_DIR, "face_recognition_sface_2021dec.onnx"),
        os.path.join(_APP_DIR, "models", "face_recognition_sface_2021dec.onnx"),
    ]
    model_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not model_path or not hasattr(cv2, "FaceRecognizerSF"):
        return None
    with _sface_lock:
        if _sface_recognizer is None or _sface_path != model_path:
            try:
                _sface_recognizer = cv2.FaceRecognizerSF.create(model_path, "")
                _sface_path = model_path
            except Exception:
                _sface_recognizer = None
                _sface_path = None
    return _sface_recognizer


def _requested_embedding_mode():
    value = os.environ.get("ERP_FACE_EMBEDDING", "").strip().lower()
    if value in ("sface", "sface_v1", "opencv_sface"):
        return "sface"
    if value in ("grayscale", "gray", "legacy"):
        return "grayscale"
    return ""


def _embedding_mode():
    meta_mode = str(_model_meta().get("embedding") or "").strip().lower()
    if meta_mode in ("sface", "grayscale"):
        return meta_mode
    requested = _requested_embedding_mode()
    if requested == "sface" and _get_sface() is not None:
        return "sface"
    return "grayscale"


def face_capabilities():
    detector = "yunet" if _get_yunet() is not None else "haar"
    embedding = _embedding_mode()
    return {
        "detector": detector,
        "embedding": embedding,
        "yunet_available": detector == "yunet",
        "sface_available": _get_sface() is not None,
        "embedding_available": embedding != "sface" or _get_sface() is not None,
        "model_path": _resolve_model_path(),
        "model_meta": _model_meta(),
    }


def _sface_matrix_from_bbox(bbox):
    x1, y1, x2, y2 = [float(v) for v in bbox]
    width = max(1.0, x2 - x1)
    height = max(1.0, y2 - y1)
    landmarks = [
        x1 + width * 0.32, y1 + height * 0.38,
        x1 + width * 0.68, y1 + height * 0.38,
        x1 + width * 0.50, y1 + height * 0.56,
        x1 + width * 0.35, y1 + height * 0.72,
        x1 + width * 0.65, y1 + height * 0.72,
    ]
    return np.array([[x1, y1, x2, y2] + landmarks], dtype=np.float32)


def _iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if not inter:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def detect_faces_bboxes(bgr_image, max_faces=8):
    """Return up to max_faces face boxes (x1,y1,x2,y2), largest first.

    Tries an optional YuNet ONNX model, then falls back to the bundled Haar
    cascade so systems without model assets keep working.
    """
    global _yunet_size
    if bgr_image is None:
        return []
    h, w = bgr_image.shape[:2]
    if h < 10 or w < 10:
        return []
    try:
        max_faces = max(1, int(max_faces))
    except Exception:
        max_faces = 8
    boxes = []
    with _detector_lock:
        detector = _get_yunet()
        if detector is not None:
            try:
                if _yunet_size != (w, h):
                    detector.setInputSize((w, h))
                    _yunet_size = (w, h)
                _, faces = detector.detect(bgr_image)
                if faces is not None:
                    for face in faces:
                        x, y, fw, fh = [int(v) for v in face[:4]]
                        x1, y1 = max(0, x), max(0, y)
                        x2, y2 = min(w, x + fw), min(h, y + fh)
                        if x2 > x1 and y2 > y1:
                            boxes.append((x1, y1, x2, y2))
            except Exception:
                boxes = []
        if not boxes:
            try:
                gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
                faces = _get_haar().detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48)
                )
                for x, y, fw, fh in faces:
                    x1, y1 = max(0, int(x)), max(0, int(y))
                    x2, y2 = min(w, int(x + fw)), min(h, int(y + fh))
                    if x2 > x1 and y2 > y1:
                        boxes.append((x1, y1, x2, y2))
            except Exception:
                return []
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    kept = []
    for box in boxes:
        if any(_iou(box, prev) > 0.45 for prev in kept):
            continue
        kept.append(box)
        if len(kept) >= max_faces:
            break
    return kept


# ---- Utility: extract face crop -> small grayscale vector (embedding) ----
def crop_face_and_embed(bgr_image, bbox_or_detection):
    h, w = bgr_image.shape[:2]
    # Support legacy mediapipe detection object
    if not (isinstance(bbox_or_detection, (tuple, list)) and len(bbox_or_detection) == 4):
        try:
            detection = bbox_or_detection
            bbox = detection.location_data.relative_bounding_box
            x1 = int(max(0, bbox.xmin * w))
            y1 = int(max(0, bbox.ymin * h))
            x2 = int(min(w, (bbox.xmin + bbox.width) * w))
            y2 = int(min(h, (bbox.ymin + bbox.height) * h))
        except Exception:
            return None
    else:
        x1, y1, x2, y2 = [int(v) for v in bbox_or_detection]
        x1 = max(0, min(x1, w - 1)); y1 = max(0, min(y1, h - 1))
        x2 = max(0, min(x2, w)); y2 = max(0, min(y2, h))
    if x2 <= x1 or y2 <= y1:
        return None
    if _embedding_mode() == "sface":
        recognizer = _get_sface()
        if recognizer is None:
            return None
        try:
            aligned = recognizer.alignCrop(bgr_image, _sface_matrix_from_bbox((x1, y1, x2, y2)))
            if aligned is None or aligned.size == 0:
                return None
            feature = recognizer.feature(aligned)
            if feature is None:
                return None
            return np.asarray(feature, dtype=np.float32).flatten()
        except Exception:
            return None
    face = bgr_image[y1:y2, x1:x2]
    face = cv2.cvtColor(face, cv2.COLOR_BGR2GRAY)
    face = cv2.resize(face, (32, 32), interpolation=cv2.INTER_AREA)
    emb = face.flatten().astype(np.float32) / 255.0
    return emb


def extract_embedding_for_image(stream_or_bytes):
    # accepts a file-like stream (werkzeug FileStorage.stream)
    try:
        if hasattr(stream_or_bytes, "read"):
            data = stream_or_bytes.read()
            try:
                stream_or_bytes.seek(0)
            except Exception:
                pass
        elif isinstance(stream_or_bytes, (bytes, bytearray)):
            data = bytes(stream_or_bytes)
        else:
            return None
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        bbox = _detect_face_bbox(img)
        if bbox is None:
            return None
        return crop_face_and_embed(img, bbox)
    except Exception:
        return None

# ---- Load model helpers ----
def load_model_if_exists():
    path = _resolve_model_path()
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)

def predict_with_model(clf, emb):
    # returns label and confidence (max probability)
    proba = clf.predict_proba([emb])[0]
    idx = np.argmax(proba)
    label = clf.classes_[idx]
    conf = float(proba[idx])
    return label, conf

# ---- Training function used in background ----
def train_model_background(dataset_dir, progress_callback=None):
    """
    dataset_dir/
        student_id/
            img1.jpg
            img2.jpg
    progress_callback(progress_percent, message) -> optional
    """
    X = []
    y = []
    embedding_mode = _embedding_mode()
    detector_mode = "yunet" if _get_yunet() is not None else "haar"
    student_dirs = [d for d in os.listdir(dataset_dir) if os.path.isdir(os.path.join(dataset_dir, d))]
    total_students = max(1, len(student_dirs))
    processed = 0

    for sid in student_dirs:
        folder = os.path.join(dataset_dir, sid)
        files = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        for fn in files:
            path = os.path.join(folder, fn)
            img = cv2.imread(path)
            if img is None:
                continue
            bbox = _detect_face_bbox(img)
            if bbox is None:
                continue
            emb = crop_face_and_embed(img, bbox)
            if emb is None:
                continue
            X.append(emb)
            try:
                y.append(int(sid))
            except Exception:
                continue
        processed += 1
        if progress_callback:
            pct = int((processed / total_students) * 80)  # up to 80% during feature extraction
            progress_callback(pct, f"Processed {processed}/{total_students} students")

    if len(X) == 0:
        msg = "No training data found (no faces detected in dataset images)"
        if progress_callback:
            progress_callback(0, msg)
        try:
            status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_status.json")
            with open(status_path, "w") as f:
                json.dump({"running": False, "progress": 0, "message": msg}, f)
        except Exception:
            pass
        return

    X = np.stack(X)
    y = np.array(y)

    if progress_callback:
        progress_callback(85, f"Training RandomForest ({embedding_mode}) on {len(y)} samples...")
    clf = RandomForestClassifier(n_estimators=150, n_jobs=-1, random_state=42)
    clf.fit(X, y)

    with open(_resolve_model_path(), "wb") as f:
        pickle.dump(clf, f)
    with open(FACE_MODEL_META_PATH, "w", encoding="utf-8") as f:
        json.dump({"embedding": embedding_mode, "detector": detector_mode}, f)

    if progress_callback:
        progress_callback(100, "Training complete")
    try:
        status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_status.json")
        with open(status_path, "w") as f:
            json.dump({"running": False, "progress": 100, "message": "Training complete"}, f)
    except Exception:
        pass
