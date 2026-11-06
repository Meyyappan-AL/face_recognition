import os
import json
import cv2
import numpy as np
import pickle
from sklearn.ensemble import RandomForestClassifier

MODEL_PATH = "model.pkl"

_haar_cascade = None
_mp_face_detector = None
_mp_detector_kind = None  # 'solutions' | 'tasks' | None


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
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH, "rb") as f:
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
        progress_callback(85, f"Training RandomForest on {len(y)} samples...")
    clf = RandomForestClassifier(n_estimators=150, n_jobs=-1, random_state=42)
    clf.fit(X, y)

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(clf, f)

    if progress_callback:
        progress_callback(100, "Training complete")
    try:
        status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_status.json")
        with open(status_path, "w") as f:
            json.dump({"running": False, "progress": 100, "message": "Training complete"}, f)
    except Exception:
        pass
