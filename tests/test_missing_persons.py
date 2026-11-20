"""Safety-gate tests for the missing-persons pipeline.

No real faces are used: images are synthetic noise PNGs and the embedding step is replaced by a mock that
maps each image to a chosen vector, so similarity is fully controlled.
"""
import hashlib
import importlib
import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

DIM = 512
_RNG = np.random.default_rng(7)
REGISTRY: dict[str, np.ndarray] = {}


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def identity(seed):
    return unit(np.random.default_rng(seed).normal(size=DIM))


def near(base, noise=0.25, seed=0):
    """A different photo of the same 'person': cosine ~0.97 with noise .25, ~0.6 with noise 1."""
    return unit(base + noise * unit(np.random.default_rng(seed + 1000).normal(size=DIM)))


def photo(vec) -> bytes:
    """Synthetic image (random pixels, no face) whose mock embedding is `vec`."""
    arr = _RNG.integers(0, 255, (48, 48, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    data = buf.getvalue()
    REGISTRY[hashlib.sha256(data).hexdigest()] = unit(vec)
    return data


def mock_embedder(data: bytes):
    from app.missing import NoFace
    e = REGISTRY.get(hashlib.sha256(data).hexdigest())
    if e is None:
        raise NoFace("No face detected in the photo")
    return e


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACEID_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FACEID_PASSWORD", "testpw")
    monkeypatch.setenv("FACEID_MP_ADMIN_PASSWORD", "admin-pass-123")
    monkeypatch.setenv("FACEID_MP_RETENTION_DAYS", "30")
    monkeypatch.delenv("FACEID_MP_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("FACEID_MP_ANTISPAM", "0")  # spam gates are covered in test_mp_antispam.py
    import app.main as m
    importlib.reload(m)
    import app.missing as mp
    monkeypatch.setattr(mp, "embedder", mock_embedder)

    def client(username=None, password=None):
        c = TestClient(m.app)
        if username:
            r = c.post("/api/mp/login", data={"username": username, "password": password})
            assert r.status_code == 200, r.text
        return c

    admin = client("admin", "admin-pass-123")
    for name, role in (("rev1", "reviewer"), ("fam1", "family"), ("fam2", "family")):
        r = admin.post("/api/mp/admin/users", data={"username": name, "password": "password-" + name, "role": role})
        assert r.status_code == 200, r.text
    return {"mp": mp, "admin": admin, "rev": client("rev1", "password-rev1"),
            "fam": client("fam1", "password-fam1"), "fam2": client("fam2", "password-fam2"),
            "public": client(), "client": client}


def new_case(fam, vecs, name="Jane Doe"):
    files = [("files", (f"p{i}.png", photo(v), "image/png")) for i, v in enumerate(vecs)]
    r = fam.post("/api/mp/cases", data={"person_name": name, "last_seen_text": "Central station"}, files=files)
    assert r.status_code == 200, r.text
    return r.json()["case"]


def sighting(c, vec, lat=47.6062, lon=-122.3321, where="5th & Pine, Seattle"):
    r = c.post("/api/mp/sightings", files={"file": ("s.png", photo(vec), "image/png")},
               data={"consent": "true", "lat": lat, "lon": lon, "location_text": where,
                     "submitter_contact": "volunteer@example.org"})
    assert r.status_code == 200, r.text
    return r.json()


def verify(admin, case, vref="SPD-2026-001234"):
    r = admin.post(f"/api/mp/admin/cases/{case['id']}/verify", data={"verification_ref": vref})
    assert r.status_code == 200, r.text
    return r.json()


def queue(rev):
    r = rev.get("/api/mp/review/queue")
    assert r.status_code == 200
    return r.json()


# ---------------- gates ----------------
def test_unverified_case_is_never_matched(env):
    p = identity(1)
    case = new_case(env["fam"], [p])
    assert case["status"] == "pending"
    sighting(env["public"], near(p, seed=1))
    assert queue(env["rev"])["matches"] == []
    mp = env["mp"]
    assert mp.S.one("SELECT COUNT(*) n FROM matches")["n"] == 0
    # rejected case: also never matched, and its embeddings are dropped
    env["admin"].post(f"/api/mp/admin/cases/{case['id']}/reject", data={"note": "no police report"})
    sighting(env["public"], near(p, seed=2))
    assert mp.S.one("SELECT COUNT(*) n FROM matches")["n"] == 0
    assert mp.S.one("SELECT COUNT(*) n FROM case_photos WHERE embedding IS NOT NULL")["n"] == 0


def test_verify_requires_reference_and_admin(env):
    case = new_case(env["fam"], [identity(2)])
    assert env["rev"].post(f"/api/mp/admin/cases/{case['id']}/verify", data={"verification_ref": "X-1"}).status_code == 403
    assert env["fam"].post(f"/api/mp/admin/cases/{case['id']}/verify", data={"verification_ref": "X-1"}).status_code == 403
    assert env["admin"].post(f"/api/mp/admin/cases/{case['id']}/verify", data={"verification_ref": " "}).status_code == 400
    verify(env["admin"], case)
    c = env["mp"].S.one("SELECT * FROM cases WHERE id=?", (case["id"],))
    assert c["status"] == "verified" and c["verification_ref"] == "SPD-2026-001234"


def test_matching_both_directions_threshold_and_ranking(env):
    a, b = identity(3), identity(4)
    # sighting submitted BEFORE the case is verified -> matched when the case goes live
    sighting(env["public"], near(a, noise=0.6, seed=10))
    case_a = new_case(env["fam"], [a])
    assert verify(env["admin"], case_a)["new_matches"] == 1
    # then new sightings are matched against the live case
    sighting(env["public"], near(a, noise=0.3, seed=11))
    sighting(env["public"], b)                       # different person -> below threshold
    q = queue(env["rev"])["matches"]
    assert len(q) == 2 and all(m["case_id"] == case_a["id"] for m in q)
    assert q[0]["score"] >= q[1]["score"]            # ranked by similarity
    assert all(m["score"] >= env["mp"].CFG.threshold for m in q)


def test_dedup_near_identical_sightings(env):
    a = identity(5)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    s1 = near(a, noise=0.4, seed=20)
    sighting(env["public"], s1)
    sighting(env["public"], near(s1, noise=0.05, seed=21))  # burst / same photo re-uploaded
    q = queue(env["rev"])["matches"]
    assert len(q) == 1 and q[0]["duplicates"] == 1


def test_no_location_leak_before_approval(env):
    a = identity(6)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    resp = env["public"].post("/api/mp/sightings", files={"file": ("s.png", photo(near(a, seed=30)), "image/png")},
                              data={"consent": "true", "lat": 47.6062, "lon": -122.3321, "location_text": "5th & Pine"})
    # submitter learns nothing about matches
    assert set(resp.json()) == {"ok", "id", "message"} and "match" not in resp.text.lower()
    fam = env["fam"]
    def owner_text():
        return fam.get("/api/mp/my/cases").text + fam.get(f"/api/mp/my/cases/{case['id']}/updates").text
    assert "47.6062" not in owner_text() and "5th" not in owner_text()
    assert fam.get(f"/api/mp/my/cases/{case['id']}/updates").json()["updates"] == []
    # notifications / webhook payloads never carry location
    notes = env["mp"].S.q("SELECT body FROM notifications")
    assert notes and not any("47.6" in n["body"] or "Pine" in n["body"] for n in notes)
    # reviewer rejects -> still nothing
    m = queue(env["rev"])["matches"][0]
    assert env["rev"].post(f"/api/mp/review/matches/{m['id']}", data={"decision": "reject"}).status_code == 200
    assert "47.6062" not in owner_text()
    # second sighting, approved -> location released to owner only
    sighting(env["public"], near(a, seed=31), lat=45.5, lon=-122.6, where="Pioneer Square")
    m2 = queue(env["rev"])["matches"][0]
    assert env["rev"].post(f"/api/mp/review/matches/{m2['id']}", data={"decision": "approve"}).status_code == 200
    ups = fam.get(f"/api/mp/my/cases/{case['id']}/updates").json()["updates"]
    assert len(ups) == 1 and ups[0]["lat"] == 45.5 and ups[0]["location_text"] == "Pioneer Square"
    assert "47.6062" not in owner_text()  # the rejected one stays hidden
    # another family can't see it
    assert env["fam2"].get(f"/api/mp/my/cases/{case['id']}/updates").status_code == 404
    assert "45.5" not in env["fam2"].get("/api/mp/my/cases").text


def test_case_owner_cannot_see_unreviewed_data(env):
    a = identity(7)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    sighting(env["public"], near(a, seed=40))
    fam, public = env["fam"], env["public"]
    m = queue(env["rev"])["matches"][0]
    assert fam.get("/api/mp/review/queue").status_code == 403
    assert public.get("/api/mp/review/queue").status_code == 401
    assert fam.post(f"/api/mp/review/matches/{m['id']}", data={"decision": "approve"}).status_code == 403
    assert fam.get(f"/api/mp/files/sighting/{m['sighting_id']}").status_code == 404
    assert public.get(f"/api/mp/files/sighting/{m['sighting_id']}").status_code == 401
    assert fam.get("/api/mp/admin/audit").status_code == 403
    assert fam.get("/api/mp/my/cases").json()["cases"][0]["approved_sightings"] == 0
    # own case photo is fine, someone else's is not
    cp = fam.get("/api/mp/my/cases").json()["cases"][0]["photos"][0]
    assert fam.get(cp).status_code == 200
    assert env["fam2"].get(cp).status_code == 404
    # after approval the owner can see that one sighting photo
    env["rev"].post(f"/api/mp/review/matches/{m['id']}", data={"decision": "approve"})
    assert fam.get(f"/api/mp/files/sighting/{m['sighting_id']}").status_code == 200


def test_retention_purge(env):
    mp = env["mp"]
    a = identity(8)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    keep = sighting(env["public"], near(a, seed=50))["id"]        # matched (pending) -> kept
    gone = sighting(env["public"], identity(99))["id"]            # unmatched -> expires
    fresh = sighting(env["public"], identity(98))["id"]           # unmatched but recent -> kept
    old = time.time() - 31 * 86400
    mp.S.x("UPDATE sightings SET created_at=? WHERE id IN (?,?)", (old, keep, gone))
    f = mp.S.one("SELECT image_file FROM sightings WHERE id=?", (gone,))["image_file"]
    r = env["admin"].post("/api/mp/admin/purge")
    assert r.status_code == 200 and r.json()["deleted"] == 1
    ids = {s["id"] for s in mp.S.q("SELECT id FROM sightings")}
    assert gone not in ids and keep in ids and fresh in ids
    import os
    assert not os.path.exists(os.path.join(mp.S.file_dir, f))
    assert any(a["action"] == "retention_purge" for a in env["admin"].get("/api/mp/admin/audit").json()["audit"])
    # a rejected match does not protect an old sighting
    mid = mp.S.one("SELECT id FROM matches WHERE sighting_id=?", (keep,))["id"]
    env["rev"].post(f"/api/mp/review/matches/{mid}", data={"decision": "reject"})
    assert env["admin"].post("/api/mp/admin/purge").json()["deleted"] == 1


def test_closing_case_deletes_embeddings(env):
    mp = env["mp"]
    a = identity(9)
    case = new_case(env["fam"], [a, near(a, seed=60)])
    verify(env["admin"], case)
    sid = sighting(env["public"], near(a, seed=61))["id"]
    assert mp.S.one("SELECT COUNT(*) n FROM case_photos WHERE case_id=? AND embedding IS NOT NULL", (case["id"],))["n"] == 2
    assert env["fam"].post(f"/api/mp/my/cases/{case['id']}/close", data={"reason": "found"}).status_code == 200
    assert mp.S.one("SELECT COUNT(*) n FROM case_photos WHERE case_id=? AND embedding IS NOT NULL", (case["id"],))["n"] == 0
    assert mp.S.one("SELECT embedding FROM sightings WHERE id=?", (sid,))["embedding"] is None
    assert mp.S.one("SELECT status FROM matches WHERE sighting_id=?", (sid,))["status"] == "withdrawn"
    # closed case is no longer matched
    sighting(env["public"], near(a, seed=62))
    assert queue(env["rev"])["matches"] == []


def test_self_report_holds_then_closes_and_shares_nothing(env):
    mp = env["mp"]
    a = identity(10)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    sighting(env["public"], near(a, seed=70), where="Old location")
    m1 = queue(env["rev"])["matches"][0]
    env["rev"].post(f"/api/mp/review/matches/{m1['id']}", data={"decision": "approve"})
    sighting(env["public"], near(a, seed=71), where="Newer location")
    m2 = queue(env["rev"])["matches"][0]
    # unknown refs get the identical response (no enumeration)
    r_bad = env["public"].post("/api/mp/self-report", data={"case_ref": "MP-NOPE00"})
    r = env["public"].post("/api/mp/self-report", data={"case_ref": case["ref"], "message": "I'm safe, I left on purpose"},
                           files={"file": ("me.png", photo(near(a, seed=72)), "image/png")})
    assert r.status_code == r_bad.status_code == 200 and r.json() == r_bad.json()
    q = queue(env["rev"])
    assert len(q["self_reports"]) == 1 and q["self_reports"][0]["score"] > 0.9
    # while pending: case on hold, approvals blocked
    assert q["matches"][0]["on_hold"] is True
    assert env["rev"].post(f"/api/mp/review/matches/{m2['id']}", data={"decision": "approve"}).status_code == 409
    # family cannot confirm it themselves
    rid = q["self_reports"][0]["id"]
    assert env["fam"].post(f"/api/mp/review/self-reports/{rid}", data={"decision": "confirm"}).status_code == 403
    assert env["rev"].post(f"/api/mp/review/self-reports/{rid}", data={"decision": "confirm"}).status_code == 200
    c = mp.S.one("SELECT * FROM cases WHERE id=?", (case["id"],))
    assert c["status"] == "closed" and c["close_reason"] == "self_report"
    upd = env["fam"].get(f"/api/mp/my/cases/{case['id']}/updates").json()
    assert upd["updates"] == [] and "Old location" not in env["fam"].get(f"/api/mp/my/cases/{case['id']}/updates").text
    assert env["fam"].get(f"/api/mp/files/sighting/{m1['sighting_id']}").status_code == 404
    assert mp.S.one("SELECT COUNT(*) n FROM case_photos WHERE embedding IS NOT NULL")["n"] == 0
    assert mp.S.one("SELECT image_file FROM self_reports WHERE id=?", (rid,))["image_file"] is None


def test_sighting_requires_consent_and_a_face(env):
    pub = env["public"]
    r = pub.post("/api/mp/sightings", files={"file": ("s.png", photo(identity(11)), "image/png")}, data={})
    assert r.status_code == 400
    r = pub.post("/api/mp/sightings", files={"file": ("x.png", b"\x89PNGnot-an-image", "image/png")},
                 data={"consent": "true"})
    assert r.status_code == 400  # corrupt file: rejected by the upload checks (generic message)
    no_face = io.BytesIO(); Image.new("RGB", (64, 64), (40, 40, 40)).save(no_face, "PNG")
    r = pub.post("/api/mp/sightings", files={"file": ("x.png", no_face.getvalue(), "image/png")},
                 data={"consent": "true"})
    assert r.status_code == 422  # valid image, no usable face


def test_everything_is_audited(env):
    a = identity(12)
    case = new_case(env["fam"], [a])
    verify(env["admin"], case)
    sighting(env["public"], near(a, seed=80))
    m = queue(env["rev"])["matches"][0]
    env["rev"].post(f"/api/mp/review/matches/{m['id']}", data={"decision": "approve"})
    actions = {x["action"] for x in env["admin"].get("/api/mp/admin/audit").json()["audit"]}
    for need in ("user_created", "case_created", "case_verified", "sighting_submitted", "match_created",
                 "match_approved", "login"):
        assert need in actions, need


def test_main_app_db_untouched(env, tmp_path):
    import sqlite3
    tables = {r[0] for r in sqlite3.connect(tmp_path / "faceid.sqlite3").execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cases" not in tables and "sightings" not in tables
