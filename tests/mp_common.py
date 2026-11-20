"""Shared helpers for missing-persons tests: synthetic images + mock face detector (no real faces)."""
import hashlib
import importlib
import io

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

DIM = 512
REGISTRY: dict = {}
_RNG = np.random.default_rng(11)


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def identity(seed):
    return unit(np.random.default_rng(seed).normal(size=DIM))


def near(base, noise=0.25, seed=0):
    return unit(base + noise * unit(np.random.default_rng(seed + 5000).normal(size=DIM)))


def noise_png(w=320, h=240):
    arr = _RNG.integers(0, 255, (h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def group_photo(faces, w=320, h=240):
    """faces: list of (bbox_fractions, vec, size_px, det_score). Returns PNG bytes registered with the mock."""
    data = noise_png(w, h)
    REGISTRY[hashlib.sha256(data).hexdigest()] = [
        {"bbox": list(b), "embedding": unit(v), "size": s, "det_score": d} for b, v, s, d in faces]
    return data


def photo(vec):
    data = noise_png(64, 64)
    REGISTRY[hashlib.sha256(data).hexdigest()] = unit(vec)
    return data


def mock_embedder(data: bytes):
    from app.missing import NoFace
    e = REGISTRY.get(hashlib.sha256(data).hexdigest())
    if e is None:
        raise NoFace("No face detected in the photo")
    return e


def make_env(tmp_path, monkeypatch, **env):
    base = {"FACEID_DATA_DIR": str(tmp_path), "FACEID_PASSWORD": "testpw",
            "FACEID_MP_ADMIN_PASSWORD": "admin-pass-123", "FACEID_MP_ANTISPAM": "1",
            "FACEID_MP_MIN_FILL_SECONDS": "0", "FACEID_MP_POW_BITS": "8"}
    base.update({k: str(v) for k, v in env.items()})
    for k in ("FACEID_MP_WEBHOOK_URL", "FACEID_MP_CAPTCHA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    import app.main as m
    importlib.reload(m)
    import app.missing as mp
    monkeypatch.setattr(mp, "embedder", mock_embedder)

    def client(username=None, password=None):
        c = TestClient(m.app)
        if username:
            assert c.post("/api/mp/login", data={"username": username, "password": password}).status_code == 200
        return c
    admin = client("admin", "admin-pass-123")
    for name, role in (("rev1", "reviewer"), ("fam1", "family"), ("fam2", "family")):
        assert admin.post("/api/mp/admin/users", data={"username": name, "password": "password-" + name,
                                                       "role": role}).status_code == 200
    return {"mp": mp, "m": m, "admin": admin, "rev": client("rev1", "password-rev1"),
            "fam": client("fam1", "password-fam1"), "fam2": client("fam2", "password-fam2"),
            "public": client(), "client": client}


def solve_pow(token, bits):
    if bits <= 0:
        return ""
    i = 0
    while True:
        h = int.from_bytes(hashlib.sha256(f"{token}:{i}".encode()).digest(), "big")
        if h >> (256 - bits) == 0:
            return str(i)
        i += 1


def spam_fields(c, form="sighting"):
    t = c.get(f"/api/mp/form-token?form={form}").json()
    return {"form_token": t["token"], "pow_nonce": solve_pow(t["token"], t["pow_bits"]), "website": ""}


def submit_sighting(c, data, lat=47.6, lon=-122.3, where="Pike St", extra=None, name="s.png"):
    fields = {"consent": "true", "lat": lat, "lon": lon, "location_text": where, **spam_fields(c), **(extra or {})}
    return c.post("/api/mp/sightings", files={"file": (name, data, "image/png")}, data=fields)


def new_case(fam, vecs, name="Jane Doe"):
    files = [("files", (f"p{i}.png", photo(v), "image/png")) for i, v in enumerate(vecs)]
    r = fam.post("/api/mp/cases", data={"person_name": name}, files=files)
    assert r.status_code == 200, r.text
    return r.json()["case"]


def verify(admin, case):
    r = admin.post(f"/api/mp/admin/cases/{case['id']}/verify", data={"verification_ref": "PD-2026-1"})
    assert r.status_code == 200, r.text
    return r.json()
