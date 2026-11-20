"""FaceID Web: FastAPI app for enrolling consenting people and identifying them in photos."""
from __future__ import annotations

import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import List

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .face_engine import FaceEngine, load_image
from .matcher import DEFAULT_MARGIN, DEFAULT_THRESHOLD, Gallery
from .storage import Store

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FACEID_DATA_DIR", os.path.join(os.path.dirname(BASE), "data"))
PASSWORD = os.environ.get("FACEID_PASSWORD", "")
SECRET = os.environ.get("FACEID_SECRET") or secrets.token_hex(32)
COOKIE = "faceid_session"
SESSION_MAX_AGE = 7 * 24 * 3600
MAX_UPLOAD = int(os.environ.get("FACEID_MAX_UPLOAD_MB", "15")) * 1024 * 1024

# Enrollment quality gates (see README)
MIN_FACE_PX = int(os.environ.get("FACEID_MIN_FACE_PX", "64"))
MIN_BLUR = float(os.environ.get("FACEID_MIN_BLUR", "25"))
MIN_DET_SCORE = float(os.environ.get("FACEID_MIN_DET_SCORE", "0.6"))
WARN_BLUR = 60.0
DUPLICATE_SIM = 0.97  # an enrollment this close to an existing photo of the person adds nothing


def landmark_problems(face) -> list[str]:
    """Sanity-check the 5 landmarks (eyes, nose, mouth corners) before trusting the alignment."""
    k = np.asarray(face.kps, np.float32)
    x1, y1, x2, y2 = face.bbox
    w, h = x2 - x1, y2 - y1
    if k.shape != (5, 2) or not np.isfinite(k).all():
        return ["Facial landmarks could not be found"]
    pad = 0.15
    inside = (k[:, 0] >= x1 - pad * w) & (k[:, 0] <= x2 + pad * w) & (k[:, 1] >= y1 - pad * h) & (k[:, 1] <= y2 + pad * h)
    if not inside.all():
        return ["Facial landmarks fall outside the face (bad detection)"]
    le, re_, nose, lm, rm = k
    eye_d = float(np.linalg.norm(re_ - le))
    probs = []
    if eye_d < 0.22 * w or re_[0] <= le[0] or rm[0] <= lm[0]:
        probs.append("Face is turned too far to the side; use a more frontal photo")
    else:
        mid = (le + re_) / 2
        yaw = abs(float(nose[0] - mid[0])) / eye_d
        if yaw > 0.45:
            probs.append(f"Face is turned too far to the side (yaw index {yaw:.2f}); use a more frontal photo")
        if not (max(le[1], re_[1]) < nose[1] < min(lm[1], rm[1]) + 0.05 * h):
            probs.append("Face is tilted or partly hidden (landmarks out of order)")
    return probs

@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("FACEID_LAZY_MODELS") != "1":
        engine()  # load ONNX models at startup so the first request is fast
    yield


app = FastAPI(title="FaceID Web", version="1.0.0", lifespan=lifespan)
_serializer = URLSafeTimedSerializer(SECRET, salt="faceid-auth")
store = Store(DATA_DIR)
_engine: FaceEngine | None = None
_gallery: Gallery | None = None


def engine() -> FaceEngine:
    global _engine
    if _engine is None:
        _engine = FaceEngine()
    return _engine


def gallery() -> Gallery:
    global _gallery
    if _gallery is None:
        _gallery = Gallery(store.all_embeddings())
    return _gallery


def invalidate_gallery():
    global _gallery
    _gallery = None


def current_threshold() -> float:
    return float(store.get_setting("threshold", DEFAULT_THRESHOLD))


def current_margin() -> float:
    return float(store.get_setting("margin", DEFAULT_MARGIN))


# ---------------- auth ----------------
def is_authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    tok = request.cookies.get(COOKIE)
    if not tok:
        return False
    try:
        _serializer.loads(tok, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


def require_auth(request: Request):
    if not is_authed(request):
        raise HTTPException(401, "Not authenticated")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(os.path.join(BASE, "static", "login.html"))


@app.post("/api/login")
def login(password: str = Form(...)):
    if not PASSWORD or hmac.compare_digest(password.encode(), PASSWORD.encode()):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _serializer.dumps({"t": time.time()}), max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="strict")
        return resp
    time.sleep(0.5)
    raise HTTPException(401, "Wrong password")


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/")
def index(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(BASE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/api/health")
def health():
    return {"ok": True, "models_loaded": _engine is not None, **store.stats()}


# ---------------- helpers ----------------
async def read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{f.filename}: file too large")
    if not data:
        raise HTTPException(400, f"{f.filename}: empty file")
    return data


def jpg(img: np.ndarray, q=90) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes()


def face_thumb(img: np.ndarray, bbox, size=160) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    s = max(x2 - x1, y2 - y1) * 1.5
    h, w = img.shape[:2]
    a, b = int(max(0, cx - s / 2)), int(max(0, cy - s / 2))
    c, d = int(min(w, cx + s / 2)), int(min(h, cy + s / 2))
    crop = img[b:d, a:c]
    if crop.size == 0:
        crop = img
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)


# ---------------- people / enrollment ----------------
@app.get("/api/people", dependencies=[Depends(require_auth)])
def list_people():
    return {"people": store.list_people(), "threshold": current_threshold()}


@app.post("/api/people", dependencies=[Depends(require_auth)])
def create_person(name: str = Form(...), notes: str = Form("")):
    name = name.strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "Name must be 1-100 characters")
    try:
        return store.create_person(name, notes.strip())
    except Exception:  # sqlite IntegrityError
        raise HTTPException(409, f"A person named '{name}' already exists")


@app.patch("/api/people/{pid}", dependencies=[Depends(require_auth)])
def rename_person(pid: int, name: str = Form(...)):
    if not store.get_person(pid):
        raise HTTPException(404, "Person not found")
    try:
        store.rename_person(pid, name.strip())
    except Exception:
        raise HTTPException(409, "Name already in use")
    invalidate_gallery()
    return store.get_person(pid)


@app.delete("/api/people/{pid}", dependencies=[Depends(require_auth)])
def delete_person(pid: int):
    if not store.get_person(pid):
        raise HTTPException(404, "Person not found")
    store.delete_person(pid)
    invalidate_gallery()
    return {"ok": True}


def assess_enrollment(faces, img_shape) -> tuple[object | None, list[str], list[str]]:
    """Return (chosen_face, errors, warnings) for an enrollment photo."""
    errors, warnings = [], []
    if not faces:
        return None, ["No face detected"], []
    faces = sorted(faces, key=lambda f: -(f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    face = faces[0]
    if len(faces) > 1:
        a0 = (face.bbox[2] - face.bbox[0]) * (face.bbox[3] - face.bbox[1])
        a1 = (faces[1].bbox[2] - faces[1].bbox[0]) * (faces[1].bbox[3] - faces[1].bbox[1])
        if a1 > 0.5 * a0:
            errors.append(f"{len(faces)} faces of similar size found; use a photo with only this person "
                          "(or crop it) so the right face is enrolled")
        else:
            warnings.append(f"{len(faces)} faces found; enrolled the largest one")
    q = face.quality
    if q["face_size"] < MIN_FACE_PX:
        errors.append(f"Face too small ({q['face_size']}px; need at least {MIN_FACE_PX}px)")
    if q["blur"] < MIN_BLUR:
        errors.append(f"Photo too blurry (sharpness {q['blur']:.0f}; need {MIN_BLUR:.0f}+)")
    elif q["blur"] < WARN_BLUR:
        warnings.append(f"Photo is a little soft (sharpness {q['blur']:.0f}); a sharper photo improves accuracy")
    if face.det_score < MIN_DET_SCORE:
        errors.append(f"Face not clearly visible (detector confidence {face.det_score:.2f})")
    errors.extend(landmark_problems(face))
    return face, errors, warnings


@app.post("/api/people/{pid}/photos", dependencies=[Depends(require_auth)])
async def add_photos(pid: int, files: List[UploadFile] = File(...), force: bool = Form(False)):
    person = store.get_person(pid)
    if not person:
        raise HTTPException(404, "Person not found")
    results = []
    for f in files:
        item = {"filename": f.filename, "accepted": False, "errors": [], "warnings": []}
        try:
            img = load_image(await read_upload(f))
        except (ValueError, HTTPException) as e:
            item["errors"].append(getattr(e, "detail", None) or str(e))
            results.append(item)
            continue
        faces = engine().analyze(img)
        face, errors, warnings = assess_enrollment(faces, img.shape)
        if face is not None and not errors:
            # Consistency checks against this person and against everyone else.
            own = store.person_embeddings(pid)
            if len(own):
                sim_own = float((own @ face.embedding).max())
                if sim_own >= DUPLICATE_SIM:
                    errors.append(f"Near-duplicate of a photo already enrolled (similarity {sim_own:.2f}); "
                                  "add a different photo (other angle/lighting) instead")
                elif sim_own < 0.25:
                    msg = f"Doesn't look like {person['name']}'s other photos (similarity {sim_own:.2f})"
                    (warnings if force else errors).append(msg + ("" if force else "; tick 'force' to add anyway"))
            g = gallery()
            if len(g):
                others = [m for m in g.identify(face.embedding[None], threshold=0.5, unique=False)
                          if m.person_id is not None and m.person_id != pid]
                if others:
                    warnings.append(f"This face also strongly resembles enrolled person '{others[0].name}' "
                                    f"(score {others[0].score:.2f}). Check it is the right person.")
        item["errors"], item["warnings"] = errors, warnings
        item["faces_found"] = len(faces)
        if face is not None:
            item["quality"] = face.quality
        if face is not None and not errors:
            h, w = img.shape[:2]
            s = min(1.0, 1280 / max(h, w))
            stored = cv2.resize(img, (int(w * s), int(h * s))) if s < 1 else img
            ph = store.add_photo(pid, jpg(stored), jpg(face_thumb(img, face.bbox)), face.embedding,
                                 [float(v * s) for v in face.bbox], face.quality)
            item.update(accepted=True, photo=ph)
            invalidate_gallery()
        results.append(item)
    return {"results": results, "person": store.get_person(pid)}


@app.delete("/api/photos/{phid}", dependencies=[Depends(require_auth)])
def delete_photo(phid: int):
    if not store.delete_photo(phid):
        raise HTTPException(404, "Photo not found")
    invalidate_gallery()
    return {"ok": True}


@app.get("/api/photos/{phid}/{kind}", dependencies=[Depends(require_auth)])
def get_photo_file(phid: int, kind: str):
    ph = store.get_photo(phid)
    if not ph or kind not in ("image", "thumb"):
        raise HTTPException(404, "Not found")
    return FileResponse(os.path.join(store.photo_dir, ph["image_file" if kind == "image" else "thumb_file"]),
                        media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------- identification ----------------
@app.post("/api/identify", dependencies=[Depends(require_auth)])
async def identify(files: List[UploadFile] = File(...), threshold: float | None = Form(None),
                   margin: float | None = Form(None), unique: bool = Form(True)):
    thr = current_threshold() if threshold is None else float(threshold)
    mrg = current_margin() if margin is None else float(margin)
    if not -1.0 <= thr <= 1.0:
        raise HTTPException(400, "threshold must be between -1 and 1")
    g = gallery()
    out = []
    for f in files:
        t0 = time.time()
        try:
            img = load_image(await read_upload(f))
        except (ValueError, HTTPException) as e:
            out.append({"filename": f.filename, "error": getattr(e, "detail", None) or str(e), "faces": []})
            continue
        faces = engine().analyze(img)
        matches = g.identify(np.stack([fc.embedding for fc in faces]), threshold=thr, unique=unique,
                                margin=mrg) if faces else []
        out.append({
            "filename": f.filename,
            "width": img.shape[1], "height": img.shape[0],
            # left-to-right order reads naturally in the UI
            "faces": sorted(({**fc.to_dict(), **m.to_dict()} for fc, m in zip(faces, matches)),
                            key=lambda d: d["bbox"][0]),
            "ms": int((time.time() - t0) * 1000),
        })
    return {"threshold": thr, "margin": mrg, "enrolled_people": len(g), "results": out}


# ---------------- settings / admin ----------------
@app.get("/api/settings", dependencies=[Depends(require_auth)])
def get_settings():
    return {"threshold": current_threshold(), "default_threshold": DEFAULT_THRESHOLD,
            "margin": current_margin(), "default_margin": DEFAULT_MARGIN,
            "password_protected": bool(PASSWORD), **store.stats()}


@app.post("/api/settings", dependencies=[Depends(require_auth)])
def set_settings(threshold: float = Form(...), margin: float | None = Form(None)):
    if not 0.0 <= threshold <= 1.0:
        raise HTTPException(400, "threshold must be between 0 and 1")
    store.set_setting("threshold", threshold)
    if margin is not None:
        if not 0.0 <= margin <= 0.3:
            raise HTTPException(400, "margin must be between 0 and 0.3")
        store.set_setting("margin", margin)
    return get_settings()


@app.post("/api/wipe", dependencies=[Depends(require_auth)])
def wipe(confirm: str = Form(...)):
    if confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    store.wipe()
    invalidate_gallery()
    return {"ok": True}

