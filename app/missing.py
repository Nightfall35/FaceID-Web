"""Consent-based missing-persons pipeline (verified cases <-> voluntarily submitted sightings).

Safety gates (enforced here, tested in tests/test_missing_persons.py; see docs/MISSING_PERSONS.md):
  * No scraping / bulk collection: the ONLY face sources are photos attached to a case by its owner
    and sighting photos that a person submits voluntarily with explicit consent.
  * Only cases with status 'verified' (admin-verified with a reference such as a police report no.)
    are ever matched. pending / rejected / closed cases are never matched.
  * Matching never releases a location. A match's location goes to the case owner only after a human
    reviewer approves that specific match. Webhook / email notifications never carry location or photos.
  * Case owners (role 'family') never see raw sightings or unreviewed / rejected matches.
  * Self-report: the missing person can say "I'm safe, don't locate me". A reviewer confirms; the case
    is closed with nothing shared, and while a self-report is pending no match can be approved.
  * Closing a case deletes its face embeddings (case photos and sightings only linked to it).
  * Unmatched sightings expire after FACEID_MP_RETENTION_DAYS (default 30) and are deleted.
  * Every state change is written to audit_log.
  * Group photos: every face (up to FACEID_MP_MAX_FACES, above a size / detector-score floor) gets its own
    embedding in sighting_faces. Once a photo has a match, the embeddings of the other faces (bystanders) are
    deleted immediately; a rejected face loses its embedding too. Families see bystanders blurred.
  * Public forms are protected by app/antispam.py (token, min fill time, PoW, honeypot, rate limits,
    upload checks, duplicate detection, optional hCaptcha/Turnstile).
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from typing import Callable, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageOps
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import antispam

log = logging.getLogger("faceid.missing")

ROLES = ("admin", "reviewer", "family")
DAY = 86400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','reviewer','family')),
    contact TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,                 -- public case code (posters / self-report)
    owner_id INTEGER NOT NULL REFERENCES users(id),
    person_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    last_seen_text TEXT DEFAULT '',
    last_seen_at TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','verified','rejected','closed')),
    verification_ref TEXT DEFAULT '',         -- e.g. police report number, recorded by admin
    verified_by INTEGER REFERENCES users(id),
    verified_at REAL,
    close_reason TEXT DEFAULT '',             -- found | self_report | withdrawn | other
    closed_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS case_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    image_file TEXT NOT NULL,
    embedding BLOB,                           -- NULL once the case closes
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_file TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    embedding BLOB,
    lat REAL, lon REAL,
    location_text TEXT DEFAULT '',
    seen_at TEXT DEFAULT '',
    submitter_contact TEXT DEFAULT '',
    note TEXT DEFAULT '',
    duplicate_of INTEGER REFERENCES sightings(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    width INTEGER, height INTEGER,
    dhash TEXT DEFAULT '',
    ip_hash TEXT DEFAULT ''                   -- HMAC of submitter IP (never the raw IP)
);
CREATE TABLE IF NOT EXISTS sighting_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sighting_id INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
    face_index INTEGER NOT NULL,
    bbox TEXT,                                -- JSON [x1,y1,x2,y2] as fractions of width/height; NULL = whole image
    det_score REAL,
    embedding BLOB,                           -- NULL once deleted (bystander / rejected / case closed)
    UNIQUE (sighting_id, face_index)
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    sighting_id INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','withdrawn')),
    created_at REAL NOT NULL,
    face_id INTEGER REFERENCES sighting_faces(id) ON DELETE SET NULL,
    face_index INTEGER,
    UNIQUE (case_id, sighting_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER REFERENCES matches(id) ON DELETE SET NULL,
    self_report_id INTEGER REFERENCES self_reports(id) ON DELETE SET NULL,
    reviewer_id INTEGER NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS self_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    image_file TEXT,
    score REAL,
    message TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audience TEXT NOT NULL,                   -- 'reviewer' | 'admin' | 'user:<id>'
    kind TEXT NOT NULL,
    body TEXT NOT NULL,                       -- never contains a location
    ref_id INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id INTEGER,
    actor_role TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    details TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_matches_status ON matches(status);
CREATE INDEX IF NOT EXISTS ix_sightings_created ON sightings(created_at);
CREATE INDEX IF NOT EXISTS ix_faces_sighting ON sighting_faces(sighting_id);
"""
# columns added after the first release of this module (ALTER for DBs created by it)
MIGRATIONS = {"sightings": [("width", "INTEGER"), ("height", "INTEGER"), ("dhash", "TEXT DEFAULT ''"),
                            ("ip_hash", "TEXT DEFAULT ''")],
              "matches": [("face_id", "INTEGER"), ("face_index", "INTEGER")]}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    def __init__(self):
        self.threshold = _f("FACEID_MP_THRESHOLD", 0.45)        # cosine similarity for a candidate match
        self.retention_days = _f("FACEID_MP_RETENTION_DAYS", 30)
        self.match_window_days = _f("FACEID_MP_MATCH_WINDOW_DAYS", 30)  # new case vs recent sightings
        self.dedup_sim = _f("FACEID_MP_DEDUP_SIM", 0.97)
        self.dedup_hours = _f("FACEID_MP_DEDUP_HOURS", 24)
        self.webhook_url = os.environ.get("FACEID_MP_WEBHOOK_URL", "").strip()   # disabled when empty
        self.email_to = os.environ.get("FACEID_MP_NOTIFY_EMAIL", "").strip()     # stub: logged only
        self.admin_user = os.environ.get("FACEID_MP_ADMIN_USER", "admin").strip() or "admin"
        self.admin_password = os.environ.get("FACEID_MP_ADMIN_PASSWORD", "")
        self.open_registration = os.environ.get("FACEID_MP_OPEN_REGISTRATION", "1") == "1"
        self.max_faces = int(_f("FACEID_MP_MAX_FACES", 20))          # faces embedded per sighting photo
        self.min_face_px = _f("FACEID_MP_MIN_FACE_PX", 40)          # skip smaller faces (short side, px)
        self.min_det_score = _f("FACEID_MP_MIN_DET_SCORE", 0.6)     # skip low-confidence detections


# ---------------- passwords / sessions ----------------
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000)
    return hmac.compare_digest(test.hex(), dk)


def _emb(blob) -> Optional[np.ndarray]:
    return None if blob is None else np.frombuffer(blob, np.float32).copy()


def _norm(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float32).reshape(-1)
    return v / max(float(np.linalg.norm(v)), 1e-12)


# ---------------- storage ----------------
class MPStore:
    def __init__(self, data_dir: str, cfg: Config):
        self.cfg = cfg
        self.data_dir = os.path.abspath(data_dir)
        self.file_dir = os.path.join(self.data_dir, "mp_files")
        os.makedirs(self.file_dir, exist_ok=True)
        # Separate DB file: never shares tables with faceid.sqlite3 (the enrolment gallery).
        self.db_path = os.path.join(self.data_dir, "missing_persons.sqlite3")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if have:
                for name, typ in cols:
                    if name not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def q(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        with self.lock:
            r = self.db.execute(sql, params).fetchone()
            return dict(r) if r else None

    def x(self, sql, params=()) -> int:
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur.lastrowid

    def save_file(self, data: bytes, prefix: str) -> str:
        name = f"{prefix}_{secrets.token_hex(12)}.jpg"
        with open(os.path.join(self.file_dir, name), "wb") as fh:
            fh.write(data)
        return name

    def remove_file(self, name: Optional[str]):
        if name:
            try:
                os.remove(os.path.join(self.file_dir, os.path.basename(name)))
            except FileNotFoundError:
                pass

    def audit(self, actor: Optional[dict], action: str, target_type: str = None, target_id: int = None, **details):
        self.x("INSERT INTO audit_log (ts, actor_id, actor_role, action, target_type, target_id, details) "
               "VALUES (?,?,?,?,?,?,?)",
               (time.time(), actor["id"] if actor else None, actor["role"] if actor else "public",
                action, target_type, target_id, json.dumps(details, default=str)))


# ---------------- embedding (overridable for tests) ----------------
class NoFace(ValueError):
    pass


def default_embedder(data: bytes) -> list[dict]:
    """All faces in the image via the existing InsightFace pipeline.

    Returns [{"bbox": [x1,y1,x2,y2] as fractions, "size": short side px, "det_score", "embedding"}]."""
    from . import main
    from .face_engine import load_image
    img = load_image(data)
    h, w = img.shape[:2]
    out = []
    for f in main.engine().analyze(img, det_thresh=0.45):
        x1, y1, x2, y2 = [float(v) for v in f.bbox]
        out.append({"bbox": [max(0, x1 / w), max(0, y1 / h), min(1, x2 / w), min(1, y2 / h)],
                    "size": min(x2 - x1, y2 - y1), "det_score": float(f.det_score), "embedding": f.embedding})
    return out


def detect_faces(data: bytes) -> list[dict]:
    """Run the embedder and apply the quality floor + per-photo cap. Raises NoFace if nothing usable.

    Accepts embedders returning a single vector (treated as one face covering the image) for simplicity."""
    raw = embedder(data)
    if isinstance(raw, np.ndarray):
        raw = [{"bbox": None, "size": None, "det_score": None, "embedding": raw}]
    faces = []
    for f in raw:
        if f.get("size") is not None and f["size"] < CFG.min_face_px:
            continue
        if f.get("det_score") is not None and f["det_score"] < CFG.min_det_score:
            continue
        faces.append({**f, "embedding": _norm(f["embedding"])})
    if not faces:
        raise NoFace("No clear face found in the photo")
    def area(f):
        b = f.get("bbox")
        return 1.0 if not b else (b[2] - b[0]) * (b[3] - b[1])
    faces.sort(key=area, reverse=True)
    return faces[:max(1, CFG.max_faces)]


embedder: Callable[[bytes], object] = default_embedder
GUARD: antispam.SpamGuard | None = None
S: MPStore | None = None
CFG = Config()
_serializer: URLSafeTimedSerializer | None = None
COOKIE = "mp_session"
MAX_UPLOAD = 15 * 1024 * 1024


def configure(data_dir: str, secret: str):
    """(Re)initialise module state. Called by app.main at import time."""
    global S, CFG, _serializer, GUARD
    CFG = Config()
    S = MPStore(data_dir, CFG)
    GUARD = antispam.SpamGuard(S, secret)
    _serializer = URLSafeTimedSerializer(secret, salt="faceid-missing-persons")
    if CFG.admin_password and not S.one("SELECT id FROM users WHERE role='admin'"):
        uid = S.x("INSERT INTO users (username, pw_hash, role, created_at) VALUES (?,?,?,?)",
                  (CFG.admin_user, hash_pw(CFG.admin_password), "admin", time.time()))
        S.audit(None, "bootstrap_admin", "user", uid, username=CFG.admin_user)


# ---------------- auth helpers ----------------
def current_user(request: Request) -> Optional[dict]:
    tok = request.cookies.get(COOKIE)
    if not tok or _serializer is None:
        return None
    try:
        data = _serializer.loads(tok, max_age=7 * DAY)
    except BadSignature:
        return None
    u = S.one("SELECT id, username, role, contact, active FROM users WHERE id=?", (data.get("uid"),))
    return u if u and u["active"] else None


def need(*roles):
    def dep(request: Request) -> dict:
        u = current_user(request)
        if not u:
            raise HTTPException(401, "Not signed in")
        if roles and u["role"] not in roles:
            raise HTTPException(403, "Not allowed for your role")
        return u
    return dep


async def read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large")
    if not data:
        raise HTTPException(400, "Empty file")
    return data


def embed_or_400(data: bytes) -> np.ndarray:
    """Largest usable face (case photos / self-report)."""
    try:
        return detect_faces(data)[0]["embedding"]
    except NoFace as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(400, f"Could not read image: {e}")


# ---------------- notifications ----------------
def notify(audience: str, kind: str, body: str, ref_id: int = None):
    """In-app notification + optional webhook / email stubs. Payloads never include location or photos."""
    S.x("INSERT INTO notifications (audience, kind, body, ref_id, created_at) VALUES (?,?,?,?,?)",
        (audience, kind, body, ref_id, time.time()))
    payload = {"kind": kind, "audience": audience, "ref_id": ref_id, "body": body}
    if CFG.webhook_url:
        try:
            req = urllib.request.Request(CFG.webhook_url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).close()
        except Exception as e:  # never let a hook break intake
            log.warning("webhook failed: %s", e)
    if CFG.email_to:
        log.info("[email stub] to=%s %s", CFG.email_to, payload)  # wire a real mailer here if needed


# ---------------- matching engine ----------------
def _open_case_embeddings(case_id: int | None = None):
    sql = ("SELECT cp.case_id, cp.embedding FROM case_photos cp JOIN cases c ON c.id=cp.case_id "
           "WHERE c.status='verified' AND cp.embedding IS NOT NULL")
    params = ()
    if case_id is not None:
        sql += " AND c.id=?"
        params = (case_id,)
    by_case: dict[int, list] = {}
    for r in S.q(sql, params):
        by_case.setdefault(r["case_id"], []).append(_emb(r["embedding"]))
    return {cid: np.stack(v) for cid, v in by_case.items()}


def _active_self_report(case_id: int) -> bool:
    return S.one("SELECT id FROM self_reports WHERE case_id=? AND status='pending'", (case_id,)) is not None


def _create_match(case_id: int, sighting_id: int, face: dict, score: float) -> Optional[int]:
    if S.one("SELECT id FROM matches WHERE case_id=? AND sighting_id=?", (case_id, sighting_id)):
        return None
    mid = S.x("INSERT INTO matches (case_id, sighting_id, face_id, face_index, score, created_at) VALUES (?,?,?,?,?,?)",
              (case_id, sighting_id, face["id"], face["face_index"], float(score), time.time()))
    S.audit(None, "match_created", "match", mid, case_id=case_id, sighting_id=sighting_id,
            face_index=face["face_index"], score=round(score, 4))
    notify("reviewer", "match_pending", f"New candidate match #{mid} awaiting review (score {score:.2f})", mid)
    return mid


def _live_faces(sighting_id: int) -> list[dict]:
    return S.q("SELECT id, face_index, embedding FROM sighting_faces WHERE sighting_id=? AND embedding IS NOT NULL "
               "ORDER BY face_index", (sighting_id,))


def drop_bystanders(sighting_id: int):
    """Once a photo has any match, delete embeddings of faces that are not part of a live match."""
    if not S.one("SELECT id FROM matches WHERE sighting_id=? AND status IN ('pending','approved')", (sighting_id,)):
        return
    n = S.one("SELECT COUNT(*) n FROM sighting_faces WHERE sighting_id=? AND embedding IS NOT NULL AND id NOT IN "
              "(SELECT face_id FROM matches WHERE sighting_id=? AND status IN ('pending','approved') "
              "AND face_id IS NOT NULL)", (sighting_id, sighting_id))["n"]
    if n:
        S.x("UPDATE sighting_faces SET embedding=NULL WHERE sighting_id=? AND id NOT IN "
            "(SELECT face_id FROM matches WHERE sighting_id=? AND status IN ('pending','approved') "
            "AND face_id IS NOT NULL)", (sighting_id, sighting_id))
        S.audit(None, "bystander_embeddings_deleted", "sighting", sighting_id, faces=n)


def _best_face(case_embs: np.ndarray, faces: list[dict]):
    best, best_face = -2.0, None
    for f in faces:
        sc = float((case_embs @ _emb(f["embedding"])).max())
        if sc > best:
            best, best_face = sc, f
    return best, best_face


def match_sighting(sighting_id: int) -> list[int]:
    s = S.one("SELECT id, duplicate_of FROM sightings WHERE id=?", (sighting_id,))
    faces = _live_faces(sighting_id) if s and not s["duplicate_of"] else []
    out = []
    if not faces:
        return out
    for cid, embs in _open_case_embeddings().items():
        score, face = _best_face(embs, faces)  # one match per (case, photo): the best face
        if score >= CFG.threshold:
            mid = _create_match(cid, sighting_id, face, score)
            if mid:
                out.append(mid)
    drop_bystanders(sighting_id)
    return out


def match_case(case_id: int) -> list[int]:
    embs = _open_case_embeddings(case_id).get(case_id)
    if embs is None:
        return []
    since = time.time() - CFG.match_window_days * DAY
    out = []
    for s in S.q("SELECT id FROM sightings WHERE duplicate_of IS NULL AND created_at >= ?", (since,)):
        faces = _live_faces(s["id"])
        if not faces:
            continue
        score, face = _best_face(embs, faces)
        if score >= CFG.threshold:
            mid = _create_match(case_id, s["id"], face, score)
            if mid:
                out.append(mid)
                drop_bystanders(s["id"])
    return out


def find_duplicate(sha: str, faces: list[dict]) -> Optional[int]:
    """Same file, or every face nearly identical to a face of one recent sighting (burst / re-upload)."""
    since = time.time() - CFG.dedup_hours * 3600
    r = S.one("SELECT id FROM sightings WHERE image_sha256=? AND created_at>=? AND duplicate_of IS NULL "
              "ORDER BY id LIMIT 1", (sha, since))
    if r:
        return r["id"]
    new = np.stack([f["embedding"] for f in faces])
    for s in S.q("SELECT id FROM sightings WHERE duplicate_of IS NULL AND created_at>=? ORDER BY id DESC", (since,)):
        old = _live_faces(s["id"])
        if not old:
            continue
        sims = new @ np.stack([_emb(f["embedding"]) for f in old]).T
        if (sims.max(axis=1) >= CFG.dedup_sim).all():
            return s["id"]
    return None


def close_case(case_id: int, reason: str, actor: Optional[dict]):
    """Close a case: stop matching, withdraw unreleased matches, delete embeddings."""
    with S.lock:
        S.x("UPDATE cases SET status='closed', close_reason=?, closed_at=? WHERE id=?", (reason, time.time(), case_id))
        S.x("UPDATE matches SET status='withdrawn' WHERE case_id=? AND status='pending'", (case_id,))
        if reason == "self_report":
            # The person asked not to be located: nothing stays shared, even previously approved matches.
            S.x("UPDATE matches SET status='withdrawn' WHERE case_id=?", (case_id,))
        S.x("UPDATE case_photos SET embedding=NULL WHERE case_id=?", (case_id,))
        # Faces matched to this case lose their embedding unless another live case still needs them.
        S.x("UPDATE sighting_faces SET embedding=NULL WHERE id IN (SELECT face_id FROM matches WHERE case_id=?) "
            "AND id NOT IN (SELECT m.face_id FROM matches m JOIN cases c ON c.id=m.case_id "
            "WHERE c.id<>? AND c.status IN ('verified','pending') AND m.status IN ('pending','approved') "
            "AND m.face_id IS NOT NULL)", (case_id, case_id))
        S.x("UPDATE sightings SET embedding=NULL WHERE id IN (SELECT sighting_id FROM matches WHERE case_id=?)",
            (case_id,))
    S.audit(actor, "case_closed", "case", case_id, reason=reason, embeddings_deleted=True)


def purge_expired(actor: Optional[dict] = None) -> int:
    """Delete sightings (and all their face embeddings) older than the retention window with no live match."""
    cutoff = time.time() - CFG.retention_days * DAY
    rows = S.q("SELECT id, image_file FROM sightings WHERE created_at < ? AND id NOT IN "
               "(SELECT sighting_id FROM matches WHERE status IN ('pending','approved'))", (cutoff,))
    for r in rows:
        S.remove_file(r["image_file"])
        S.x("DELETE FROM sighting_faces WHERE sighting_id=?", (r["id"],))
        S.x("DELETE FROM sightings WHERE id=?", (r["id"],))
    if rows:
        S.audit(actor, "retention_purge", "sighting", None, deleted=len(rows), retention_days=CFG.retention_days)
    return len(rows)


# ---------------- images ----------------
def normalise_image(data: bytes) -> tuple[bytes, int, int]:
    """Re-encode as JPEG with EXIF orientation applied and ALL metadata (incl. GPS EXIF) stripped.

    Same geometry as face_engine.load_image, so fractional face boxes line up."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if max(im.size) > 2000:
        im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue(), im.size[0], im.size[1]


def _read_bgr(name: str) -> np.ndarray:
    img = cv2.imread(os.path.join(S.file_dir, os.path.basename(name)), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(404, "Not found")
    return img


def _px(bbox, w, h, pad=0.0):
    x1, y1, x2, y2 = bbox
    bw, bh = (x2 - x1) * w, (y2 - y1) * h
    return (int(max(0, x1 * w - pad * bw)), int(max(0, y1 * h - pad * bh)),
            int(min(w, x2 * w + pad * bw)), int(min(h, y2 * h + pad * bh)))


def render_blurred(sighting_id: int, keep_face_ids: set) -> bytes:
    """Sighting photo with every face except `keep_face_ids` heavily blurred (what families see)."""
    s = S.one("SELECT image_file FROM sightings WHERE id=?", (sighting_id,))
    img = _read_bgr(s["image_file"])
    h, w = img.shape[:2]
    for f in S.q("SELECT id, bbox FROM sighting_faces WHERE sighting_id=?", (sighting_id,)):
        if f["id"] in keep_face_ids or not f["bbox"]:
            continue
        x1, y1, x2, y2 = _px(json.loads(f["bbox"]), w, h, pad=0.25)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        roi = img[y1:y2, x1:x2]
        small = cv2.resize(roi, (max(1, (x2 - x1) // 16), max(1, (y2 - y1) // 16)), interpolation=cv2.INTER_AREA)
        img[y1:y2, x1:x2] = cv2.GaussianBlur(cv2.resize(small, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LINEAR),
                                             (0, 0), 3)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def render_face_crop(sighting_id: int, face_id: Optional[int], size=256) -> bytes:
    s = S.one("SELECT image_file FROM sightings WHERE id=?", (sighting_id,))
    img = _read_bgr(s["image_file"])
    f = S.one("SELECT bbox FROM sighting_faces WHERE id=?", (face_id,)) if face_id else None
    if f and f["bbox"]:
        h, w = img.shape[:2]
        x1, y1, x2, y2 = _px(json.loads(f["bbox"]), w, h, pad=0.35)
        img = img[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else img
    sc = size / max(img.shape[:2])
    img = cv2.resize(img, (max(1, int(img.shape[1] * sc)), max(1, int(img.shape[0] * sc))), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


# ---------------- serialisers ----------------
def case_public(c: dict) -> dict:
    keys = ("id", "ref", "person_name", "description", "last_seen_text", "last_seen_at", "status",
            "verification_ref", "close_reason", "created_at", "verified_at", "closed_at")
    return {k: c.get(k) for k in keys}


def photo_url(kind: str, pid: int) -> str:
    return f"/api/mp/files/{kind}/{pid}"


router = APIRouter(prefix="/api/mp")


# ---------------- account endpoints ----------------
@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    u = S.one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not u or not u["active"] or not check_pw(password, u["pw_hash"]):
        time.sleep(0.3)
        S.audit(None, "login_failed", "user", None, username=username.strip()[:60])
        raise HTTPException(401, "Wrong username or password")
    resp = JSONResponse({"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"]}})
    resp.set_cookie(COOKIE, _serializer.dumps({"uid": u["id"]}), max_age=int(7 * DAY), httponly=True,
                    samesite="strict")
    S.audit(u, "login", "user", u["id"])
    return resp


@router.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/me")
def me(request: Request):
    u = current_user(request)
    return {"user": None if not u else {"id": u["id"], "username": u["username"], "role": u["role"]}}


def _new_user(username: str, password: str, role: str, contact: str = "") -> int:
    username = username.strip()
    if not (3 <= len(username) <= 40) or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "Username: 3-40 letters, digits, . _ -")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if role not in ROLES:
        raise HTTPException(400, "Bad role")
    try:
        return S.x("INSERT INTO users (username, pw_hash, role, contact, created_at) VALUES (?,?,?,?,?)",
                   (username, hash_pw(password), role, contact.strip()[:200], time.time()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username taken")


@router.post("/register")
def register(username: str = Form(...), password: str = Form(...), contact: str = Form("")):
    """Families can self-register; their cases still need admin verification before anything is matched."""
    if not CFG.open_registration:
        raise HTTPException(403, "Registration is closed; ask an administrator for an account")
    uid = _new_user(username, password, "family", contact)
    S.audit({"id": uid, "role": "family"}, "user_registered", "user", uid)
    return {"ok": True, "id": uid}


@router.post("/admin/users")
def admin_create_user(username: str = Form(...), password: str = Form(...), role: str = Form(...),
                      contact: str = Form(""), admin: dict = Depends(need("admin"))):
    uid = _new_user(username, password, role, contact)
    S.audit(admin, "user_created", "user", uid, role=role, username=username.strip())
    return {"ok": True, "id": uid}


@router.get("/admin/users")
def admin_list_users(admin: dict = Depends(need("admin"))):
    return {"users": S.q("SELECT id, username, role, contact, active, created_at FROM users ORDER BY id")}


@router.post("/admin/users/{uid}/active")
def admin_set_active(uid: int, active: bool = Form(...), admin: dict = Depends(need("admin"))):
    if uid == admin["id"]:
        raise HTTPException(400, "You cannot deactivate yourself")
    S.x("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    S.audit(admin, "user_active", "user", uid, active=active)
    return {"ok": True}


# ---------------- cases (family / case owner) ----------------
@router.post("/cases")
async def create_case(request: Request, person_name: str = Form(...), description: str = Form(""), last_seen_text: str = Form(""),
                      last_seen_at: str = Form(""), files: List[UploadFile] = File(...),
                      user: dict = Depends(need("family", "admin"))):
    person_name = person_name.strip()
    if not 1 <= len(person_name) <= 120:
        raise HTTPException(400, "Name must be 1-120 characters")
    if not files or len(files) > 10:
        raise HTTPException(400, "Attach 1-10 photos of the missing person")
    embs = []
    for f in files:
        data = await read_upload(f)
        GUARD.check_image(request, "case", data, S.audit)
        embs.append((normalise_image(data)[0], embed_or_400(data)))
    ref = "MP-" + secrets.token_hex(3).upper()
    cid = S.x("INSERT INTO cases (ref, owner_id, person_name, description, last_seen_text, last_seen_at, created_at) "
              "VALUES (?,?,?,?,?,?,?)", (ref, user["id"], person_name, description.strip()[:2000],
                                          last_seen_text.strip()[:300], last_seen_at.strip()[:40], time.time()))
    for data, e in embs:
        S.x("INSERT INTO case_photos (case_id, image_file, embedding, created_at) VALUES (?,?,?,?)",
            (cid, S.save_file(data, "case"), e.astype(np.float32).tobytes(), time.time()))
    S.audit(user, "case_created", "case", cid, photos=len(embs))
    notify("admin", "case_pending", f"Case {ref} awaits verification", cid)
    return {"ok": True, "case": case_public(S.one("SELECT * FROM cases WHERE id=?", (cid,)))}


def _owned_case(case_id: int, user: dict) -> dict:
    c = S.one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not c or (user["role"] == "family" and c["owner_id"] != user["id"]):
        raise HTTPException(404, "Case not found")
    return c


@router.get("/my/cases")
def my_cases(user: dict = Depends(need("family"))):
    rows = S.q("SELECT * FROM cases WHERE owner_id=? ORDER BY id DESC", (user["id"],))
    out = []
    for c in rows:
        d = case_public(c)
        d["photos"] = [photo_url("case", p["id"]) for p in
                       S.q("SELECT id FROM case_photos WHERE case_id=?", (c["id"],))]
        # Only reviewer-approved matches, and only the count; details via /my/cases/{id}/updates.
        d["approved_sightings"] = 0 if c["close_reason"] == "self_report" else S.one(
            "SELECT COUNT(*) n FROM matches WHERE case_id=? AND status='approved'", (c["id"],))["n"]
        out.append(d)
    return {"cases": out}


@router.get("/my/cases/{case_id}/updates")
def my_case_updates(case_id: int, user: dict = Depends(need("family"))):
    """The ONLY path by which a location reaches a case owner: approved matches on a case not self-closed."""
    c = _owned_case(case_id, user)
    if c["close_reason"] == "self_report":
        return {"case": case_public(c), "updates": [],
                "message": "This case was closed at the person's request. No information is shared."}
    rows = S.q("SELECT m.id, m.score, s.id sid, s.lat, s.lon, s.location_text, s.seen_at, "
               "(SELECT created_at FROM reviews r WHERE r.match_id=m.id AND r.decision='approve' "
               " ORDER BY r.id DESC LIMIT 1) approved_at "
               "FROM matches m JOIN sightings s ON s.id=m.sighting_id "
               "WHERE m.case_id=? AND m.status='approved' ORDER BY m.id DESC", (case_id,))
    S.audit(user, "owner_viewed_updates", "case", case_id, n=len(rows))
    return {"case": case_public(c), "updates": [
        {"match_id": r["id"], "approved_at": r["approved_at"], "seen_at": r["seen_at"], "lat": r["lat"],
         "lon": r["lon"], "location_text": r["location_text"], "photo": photo_url("sighting", r["sid"]),
         "face": f"/api/mp/files/face/{r['id']}"}
        for r in rows]}


@router.post("/my/cases/{case_id}/close")
def owner_close(case_id: int, reason: str = Form("found"), user: dict = Depends(need("family"))):
    c = _owned_case(case_id, user)
    if c["status"] == "closed":
        raise HTTPException(409, "Already closed")
    close_case(case_id, reason if reason in ("found", "withdrawn") else "other", user)
    return {"ok": True}


# ---------------- admin: verification ----------------
@router.get("/admin/cases")
def admin_cases(status: str = "pending", admin: dict = Depends(need("admin"))):
    rows = S.q("SELECT c.*, u.username owner, u.contact owner_contact FROM cases c JOIN users u ON u.id=c.owner_id "
               + ("WHERE c.status=? " if status != "all" else "") + "ORDER BY c.id DESC",
               (status,) if status != "all" else ())
    for r in rows:
        r.pop("owner_id", None)
        r["photos"] = [photo_url("case", p["id"]) for p in S.q("SELECT id FROM case_photos WHERE case_id=?", (r["id"],))]
    return {"cases": rows}


@router.post("/admin/cases/{case_id}/verify")
def admin_verify(case_id: int, verification_ref: str = Form(...), admin: dict = Depends(need("admin"))):
    c = S.one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not c:
        raise HTTPException(404, "Case not found")
    if c["status"] != "pending":
        raise HTTPException(409, f"Case is {c['status']}, not pending")
    vref = verification_ref.strip()
    if len(vref) < 3:
        raise HTTPException(400, "Record a verification reference (e.g. police report number)")
    S.x("UPDATE cases SET status='verified', verification_ref=?, verified_by=?, verified_at=? WHERE id=?",
        (vref[:120], admin["id"], time.time(), case_id))
    S.audit(admin, "case_verified", "case", case_id, verification_ref=vref[:120])
    notify(f"user:{c['owner_id']}", "case_verified", f"Case {c['ref']} is verified and active", case_id)
    new = match_case(case_id)  # automatic: new case vs recent sightings
    return {"ok": True, "new_matches": len(new)}


@router.post("/admin/cases/{case_id}/reject")
def admin_reject(case_id: int, note: str = Form(""), admin: dict = Depends(need("admin"))):
    c = S.one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not c or c["status"] != "pending":
        raise HTTPException(409, "Only pending cases can be rejected")
    S.x("UPDATE cases SET status='rejected' WHERE id=?", (case_id,))
    S.x("UPDATE case_photos SET embedding=NULL WHERE case_id=?", (case_id,))
    S.audit(admin, "case_rejected", "case", case_id, note=note[:500], embeddings_deleted=True)
    notify(f"user:{c['owner_id']}", "case_rejected", f"Case {c['ref']} could not be verified", case_id)
    return {"ok": True}


@router.post("/admin/cases/{case_id}/close")
def admin_close(case_id: int, reason: str = Form("other"), admin: dict = Depends(need("admin"))):
    c = S.one("SELECT * FROM cases WHERE id=?", (case_id,))
    if not c or c["status"] == "closed":
        raise HTTPException(409, "Case not open")
    close_case(case_id, reason if reason in ("found", "withdrawn", "other") else "other", admin)
    return {"ok": True}


@router.post("/admin/purge")
def admin_purge(admin: dict = Depends(need("admin"))):
    return {"deleted": purge_expired(admin), "retention_days": CFG.retention_days}


@router.get("/admin/audit")
def admin_audit(limit: int = 200, admin: dict = Depends(need("admin"))):
    rows = S.q("SELECT a.*, u.username actor FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id "
               "ORDER BY a.id DESC LIMIT ?", (max(1, min(limit, 2000)),))
    for r in rows:
        r["details"] = json.loads(r["details"] or "{}")
    return {"audit": rows}


@router.get("/config")
def get_config(user: dict = Depends(need("admin", "reviewer"))):
    return {"threshold": CFG.threshold, "retention_days": CFG.retention_days, "max_faces": CFG.max_faces,
            "min_face_px": CFG.min_face_px, "min_det_score": CFG.min_det_score, "antispam": GUARD.cfg.enabled,
            "pow_bits": GUARD.cfg.pow_bits, "captcha": GUARD.cfg.captcha or None,
            "match_window_days": CFG.match_window_days, "dedup_sim": CFG.dedup_sim,
            "webhook_enabled": bool(CFG.webhook_url), "email_stub_enabled": bool(CFG.email_to)}


# ---------------- public: sightings & self-report ----------------
@router.get("/form-token")
def form_token(form: str = "sighting"):
    """Signed, single-use token for a public form + its proof-of-work difficulty (and captcha sitekey if any)."""
    return GUARD.issue(form)


def _spam_fields(website, form_token, pow_nonce, captcha_response):
    return {"website": website, "form_token": form_token, "pow_nonce": pow_nonce, "captcha_response": captcha_response}


@router.post("/sightings")
async def submit_sighting(request: Request, file: UploadFile = File(...), consent: bool = Form(False),
                          lat: Optional[float] = Form(None), lon: Optional[float] = Form(None),
                          location_text: str = Form(""), seen_at: str = Form(""),
                          submitter_contact: str = Form(""), note: str = Form(""),
                          website: str = Form(""), form_token: str = Form(""), pow_nonce: str = Form(""),
                          captcha_response: str = Form("")):
    """Voluntary sighting intake. The response never reveals whether it matched any case."""
    iph = GUARD.check(request, "sighting", _spam_fields(website, form_token, pow_nonce, captcha_response), S.audit)
    if not consent:
        raise HTTPException(400, "Please confirm you took this photo yourself and agree to it being compared "
                                 "against verified missing-person cases")
    if lat is not None and not -90 <= lat <= 90 or lon is not None and not -180 <= lon <= 180:
        raise HTTPException(400, "Bad coordinates")
    data = await file.read()
    meta = GUARD.check_image(request, "sighting", data, S.audit)
    if GUARD.cfg.enabled:  # same person re-posting the same / near-same picture
        since = time.time() - GUARD.cfg.dup_hours * 3600
        for r in S.q("SELECT image_sha256, dhash FROM sightings WHERE ip_hash=? AND created_at>=?", (iph, since)):
            if r["image_sha256"] == meta["sha256"] or (r["dhash"] and
                                                        antispam.hamming(r["dhash"], meta["dhash"]) <= GUARD.cfg.dup_hamming):
                GUARD.reject(request, "sighting", "duplicate_submission", S.audit)
    faces = []
    try:
        faces = detect_faces(data)
    except NoFace as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(400, f"Could not read image: {e}")
    stored, w, h = normalise_image(data)
    dup = find_duplicate(meta["sha256"], faces)
    sid = S.x("INSERT INTO sightings (image_file, image_sha256, embedding, lat, lon, location_text, seen_at, "
              "submitter_contact, note, duplicate_of, created_at, width, height, dhash, ip_hash) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (S.save_file(stored, "sight"), meta["sha256"], None, lat, lon,
               location_text.strip()[:300], seen_at.strip()[:40], submitter_contact.strip()[:200],
               note.strip()[:1000], dup, time.time(), w, h, meta["dhash"], iph))
    for i, f in enumerate(faces):
        S.x("INSERT INTO sighting_faces (sighting_id, face_index, bbox, det_score, embedding) VALUES (?,?,?,?,?)",
            (sid, i, json.dumps([round(float(v), 5) for v in f["bbox"]]) if f.get("bbox") else None,
             f.get("det_score"), None if dup else f["embedding"].astype(np.float32).tobytes()))
    actor = current_user(request)
    S.audit(actor, "sighting_submitted", "sighting", sid, duplicate_of=dup, faces=len(faces),
            has_location=lat is not None or bool(location_text.strip()))
    if dup is None:
        match_sighting(sid)  # automatic: every face vs verified open cases
    purge_expired()
    return {"ok": True, "id": sid,
            "message": "Thank you. Your sighting was received and will be checked by trained reviewers."}


@router.post("/self-report")
async def self_report(request: Request, case_ref: str = Form(...), message: str = Form(""),
                      file: UploadFile | None = File(None), website: str = Form(""), form_token: str = Form(""),
                      pow_nonce: str = Form(""), captcha_response: str = Form("")):
    """The missing person says they are safe and do not want to be located.

    Always returns the same response (no case enumeration). A reviewer must confirm; while pending,
    no match on the case can be approved."""
    GUARD.check(request, "self", _spam_fields(website, form_token, pow_nonce, captcha_response), S.audit)
    data = None
    if file is not None:
        data = await file.read()
        data = data or None
        if data:
            GUARD.check_image(request, "self", data, S.audit)
    c = S.one("SELECT * FROM cases WHERE ref=? AND status IN ('pending','verified')", (case_ref.strip().upper(),))
    score, img_name = None, None
    if c:
        if data:
            try:
                e = embed_or_400(data)
                embs = [_emb(r["embedding"]) for r in S.q("SELECT embedding FROM case_photos WHERE case_id=? "
                                                           "AND embedding IS NOT NULL", (c["id"],))]
                if embs:
                    score = float((np.stack(embs) @ e).max())
            except HTTPException:
                pass
            img_name = S.save_file(normalise_image(data)[0], "self")
        rid = S.x("INSERT INTO self_reports (case_id, image_file, score, message, created_at) VALUES (?,?,?,?,?)",
                  (c["id"], img_name, score, message.strip()[:2000], time.time()))
        S.audit(None, "self_report_submitted", "case", c["id"], self_report_id=rid)
        notify("reviewer", "self_report_pending", f"Self-report #{rid} awaits confirmation (case on hold)", rid)
    return {"ok": True, "message": "Received. A reviewer will confirm it; while it is reviewed, no sighting "
                                   "information on this case is released."}


# ---------------- reviewer ----------------
@router.get("/review/queue")
def review_queue(reviewer: dict = Depends(need("reviewer", "admin"))):
    rows = S.q("SELECT m.id, m.score, m.created_at, m.case_id, m.sighting_id, m.face_index, s.width, s.height, c.ref, c.person_name, "
               "c.last_seen_text, s.location_text, s.lat, s.lon, s.seen_at, s.note, s.submitter_contact, "
               "(SELECT COUNT(*) FROM sightings d WHERE d.duplicate_of=s.id) duplicates "
               "FROM matches m JOIN cases c ON c.id=m.case_id JOIN sightings s ON s.id=m.sighting_id "
               "WHERE m.status='pending' AND c.status='verified' ORDER BY m.score DESC, m.id")
    for r in rows:
        r["case_photos"] = [photo_url("case", p["id"]) for p in
                            S.q("SELECT id FROM case_photos WHERE case_id=?", (r["case_id"],))]
        r["sighting_photo"] = photo_url("sighting", r["sighting_id"])
        r["face_crop"] = f"/api/mp/files/face/{r['id']}"
        r["on_hold"] = _active_self_report(r["case_id"])
        faces = S.q("SELECT id, face_index, bbox FROM sighting_faces WHERE sighting_id=? ORDER BY face_index",
                    (r["sighting_id"],))
        r["faces"] = [{"face_index": f["face_index"], "bbox": json.loads(f["bbox"]) if f["bbox"] else None,
                       "matched": f["face_index"] == r["face_index"]}
                      for f in faces]
        r["face_count"] = len(faces)
    reports = S.q("SELECT sr.id, sr.case_id, sr.score, sr.message, sr.created_at, sr.image_file IS NOT NULL has_photo, "
                  "c.ref, c.person_name FROM self_reports sr JOIN cases c ON c.id=sr.case_id "
                  "WHERE sr.status='pending' ORDER BY sr.id")
    for r in reports:
        r["photo"] = photo_url("self", r["id"]) if r.pop("has_photo") else None
        r["case_photos"] = [photo_url("case", p["id"]) for p in
                            S.q("SELECT id FROM case_photos WHERE case_id=?", (r["case_id"],))]
    return {"matches": rows, "self_reports": reports, "threshold": CFG.threshold}


@router.post("/review/matches/{match_id}")
def review_match(match_id: int, decision: str = Form(...), note: str = Form(""),
                 reviewer: dict = Depends(need("reviewer", "admin"))):
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    m = S.one("SELECT m.*, c.status case_status, c.owner_id, c.ref FROM matches m JOIN cases c ON c.id=m.case_id "
              "WHERE m.id=?", (match_id,))
    if not m:
        raise HTTPException(404, "Match not found")
    if m["status"] != "pending":
        raise HTTPException(409, f"Match already {m['status']}")
    if decision == "approve":
        if m["case_status"] != "verified":
            raise HTTPException(409, "Case is not verified/open")
        if _active_self_report(m["case_id"]):
            raise HTTPException(409, "Case is on hold: a self-report is pending review")
    S.x("UPDATE matches SET status=? WHERE id=?", ("approved" if decision == "approve" else "rejected", match_id))
    S.x("INSERT INTO reviews (match_id, reviewer_id, decision, note, created_at) VALUES (?,?,?,?,?)",
        (match_id, reviewer["id"], decision, note.strip()[:1000], time.time()))
    if decision == "reject" and m["face_id"]:
        # A rejected face is a bystander: drop its embedding unless another live match still uses it.
        if not S.one("SELECT id FROM matches WHERE face_id=? AND status IN ('pending','approved')", (m["face_id"],)):
            S.x("UPDATE sighting_faces SET embedding=NULL WHERE id=?", (m["face_id"],))
    S.audit(reviewer, f"match_{decision}d", "match", match_id, case_id=m["case_id"], sighting_id=m["sighting_id"],
            location_released=decision == "approve")
    if decision == "approve":
        notify(f"user:{m['owner_id']}", "sighting_approved",
               f"A reviewer confirmed a sighting for case {m['ref']}. Sign in to see it.", m["case_id"])
    return {"ok": True}


@router.post("/review/self-reports/{rid}")
def review_self_report(rid: int, decision: str = Form(...), note: str = Form(""),
                       reviewer: dict = Depends(need("reviewer", "admin"))):
    if decision not in ("confirm", "reject"):
        raise HTTPException(400, "decision must be confirm or reject")
    r = S.one("SELECT * FROM self_reports WHERE id=?", (rid,))
    if not r or r["status"] != "pending":
        raise HTTPException(409, "Self-report not pending")
    S.x("UPDATE self_reports SET status=? WHERE id=?", ("confirmed" if decision == "confirm" else "rejected", rid))
    S.x("INSERT INTO reviews (self_report_id, reviewer_id, decision, note, created_at) VALUES (?,?,?,?,?)",
        (rid, reviewer["id"], decision, note.strip()[:1000], time.time()))
    # The self-report photo is only needed for this decision.
    S.remove_file(r["image_file"])
    S.x("UPDATE self_reports SET image_file=NULL WHERE id=?", (rid,))
    S.audit(reviewer, f"self_report_{decision}ed", "case", r["case_id"], self_report_id=rid)
    if decision == "confirm":
        close_case(r["case_id"], "self_report", reviewer)
        c = S.one("SELECT owner_id, ref FROM cases WHERE id=?", (r["case_id"],))
        notify(f"user:{c['owner_id']}", "case_closed",
               f"Case {c['ref']} has been closed. The person is reported safe; no information is shared.", r["case_id"])
    return {"ok": True}


@router.get("/notifications")
def notifications(user: dict = Depends(need())):
    aud = [f"user:{user['id']}"] + (["reviewer"] if user["role"] in ("reviewer", "admin") else []) + \
          (["admin"] if user["role"] == "admin" else [])
    marks = ",".join("?" * len(aud))
    return {"notifications": S.q(f"SELECT id, kind, body, ref_id, read, created_at FROM notifications "
                                 f"WHERE audience IN ({marks}) ORDER BY id DESC LIMIT 100", aud)}


# ---------------- files (role-checked) ----------------
@router.get("/files/{kind}/{fid}")
def get_file(kind: str, fid: int, user: dict = Depends(need())):
    if kind == "case":
        r = S.one("SELECT cp.image_file, c.owner_id FROM case_photos cp JOIN cases c ON c.id=cp.case_id WHERE cp.id=?", (fid,))
        if r and user["role"] == "family" and r["owner_id"] != user["id"]:
            r = None
    elif kind == "sighting":
        r = S.one("SELECT image_file FROM sightings WHERE id=?", (fid,))
        if r and user["role"] == "family":
            # Owners only see sighting photos of their own approved, non-self-closed matches,
            # with every other face (bystanders) blurred.
            ok = S.q("SELECT m.face_id FROM matches m JOIN cases c ON c.id=m.case_id WHERE m.sighting_id=? "
                     "AND m.status='approved' AND c.owner_id=? AND c.close_reason<>'self_report'", (fid, user["id"]))
            if not ok:
                raise HTTPException(404, "Not found")
            return Response(render_blurred(fid, {x["face_id"] for x in ok}), media_type="image/jpeg",
                            headers={"Cache-Control": "private, no-store"})
    elif kind == "face":  # crop of the matched face for a match id
        m = S.one("SELECT m.sighting_id, m.face_id, m.status, c.owner_id, c.close_reason FROM matches m "
                  "JOIN cases c ON c.id=m.case_id WHERE m.id=?", (fid,))
        allowed = m and (user["role"] in ("reviewer", "admin") or
                         (m["owner_id"] == user["id"] and m["status"] == "approved" and m["close_reason"] != "self_report"))
        if not allowed:
            raise HTTPException(404, "Not found")
        return Response(render_face_crop(m["sighting_id"], m["face_id"]), media_type="image/jpeg",
                        headers={"Cache-Control": "private, no-store"})
    elif kind == "self":
        r = S.one("SELECT image_file FROM self_reports WHERE id=?", (fid,)) if user["role"] in ("reviewer", "admin") else None
    else:
        r = None
    if not r or not r.get("image_file"):
        raise HTTPException(404, "Not found")
    path = os.path.join(S.file_dir, os.path.basename(r["image_file"]))
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
