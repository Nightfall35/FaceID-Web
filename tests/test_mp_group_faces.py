"""Group photos: every face is embedded, matches point at a face, bystander biometrics are dropped."""
import io
import json

import numpy as np
import pytest
from PIL import Image

from mp_common import group_photo, identity, make_env, near, new_case, submit_sighting, verify

B0, B1, B2 = (0.05, 0.1, 0.3, 0.6), (0.4, 0.15, 0.6, 0.55), (0.7, 0.2, 0.95, 0.6)
TINY, WEAK = (0.0, 0.8, 0.05, 0.86), (0.85, 0.75, 0.95, 0.95)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def faces_of(mp, sid):
    return mp.S.q("SELECT * FROM sighting_faces WHERE sighting_id=? ORDER BY face_index", (sid,))


def crowd(target, idx=1):
    """3 good faces (target at position idx in area order... sizes equal) + 1 tiny + 1 low-score face."""
    people = [identity(901), identity(902), identity(903)]
    people[idx] = near(target, seed=7)
    return [(B0, people[0], 90, 0.95), (B1, people[1], 90, 0.95), (B2, people[2], 90, 0.95),
            (TINY, identity(904), 12, 0.95), (WEAK, identity(905), 80, 0.3)]


def test_every_face_matched_and_bystanders_dropped(env):
    mp = env["mp"]
    target = identity(1)
    case = new_case(env["fam"], [target])
    verify(env["admin"], case)
    r = submit_sighting(env["public"], group_photo(crowd(target, idx=1)))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    faces = faces_of(mp, sid)
    assert len(faces) == 3                      # tiny + low-score faces skipped, never stored
    q = env["rev"].get("/api/mp/review/queue").json()["matches"]
    assert len(q) == 1
    m = q[0]
    matched = [f for f in m["faces"] if f["matched"]]
    assert len(matched) == 1 and matched[0]["bbox"] == list(B1) and m["face_index"] == matched[0]["face_index"]
    assert m["face_count"] == 3 and m["width"] == 320 and m["height"] == 240
    # only the matched face keeps an embedding
    live = [f for f in faces_of(mp, sid) if f["embedding"] is not None]
    assert len(live) == 1 and json.loads(live[0]["bbox"]) == list(B1)
    assert any(a["action"] == "bystander_embeddings_deleted" for a in env["admin"].get("/api/mp/admin/audit").json()["audit"])
    # reviewer gets a crop of the matched face; family does not (unapproved)
    crop = env["rev"].get(m["face_crop"])
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/jpeg"
    assert env["fam"].get(m["face_crop"]).status_code == 404
    assert env["fam"].get(m["sighting_photo"]).status_code == 404


def test_face_cap(env, monkeypatch):
    mp = env["mp"]
    monkeypatch.setattr(mp.CFG, "max_faces", 20)
    faces = []
    for i in range(25):
        x = (i % 5) / 5; y = (i // 5) / 5
        faces.append(((x, y, x + 0.15, y + 0.15), identity(1000 + i), 60, 0.9))
    sid = submit_sighting(env["public"], group_photo(faces, 400, 400)).json()["id"]
    assert len(faces_of(mp, sid)) == 20


def test_new_case_matches_face_in_older_group_photo(env):
    mp = env["mp"]
    target = identity(2)
    sid = submit_sighting(env["public"], group_photo(crowd(target, idx=2))).json()["id"]
    assert sum(f["embedding"] is not None for f in faces_of(mp, sid)) == 3  # unmatched photo: kept for new cases
    case = new_case(env["fam"], [target])
    assert verify(env["admin"], case)["new_matches"] == 1
    m = mp.S.one("SELECT * FROM matches WHERE sighting_id=?", (sid,))
    assert m["face_index"] is not None
    live = [f for f in faces_of(mp, sid) if f["embedding"] is not None]
    assert len(live) == 1 and json.loads(live[0]["bbox"]) == list(B2)


def test_family_sees_bystanders_blurred_after_approval(env):
    mp = env["mp"]
    target = identity(3)
    case = new_case(env["fam"], [target])
    verify(env["admin"], case)
    sid = submit_sighting(env["public"], group_photo(crowd(target, idx=0))).json()["id"]
    m = env["rev"].get("/api/mp/review/queue").json()["matches"][0]
    assert env["rev"].post(f"/api/mp/review/matches/{m['id']}", data={"decision": "approve"}).status_code == 200
    orig = np.asarray(Image.open(io.BytesIO(env["rev"].get(m["sighting_photo"]).content)).convert("L"), np.float32)
    fam_img = env["fam"].get(f"/api/mp/files/sighting/{sid}")
    assert fam_img.status_code == 200
    blurred = np.asarray(Image.open(io.BytesIO(fam_img.content)).convert("L"), np.float32)
    h, w = orig.shape

    def region(a, b):
        return a[int(b[1] * h) + 4:int(b[3] * h) - 4, int(b[0] * w) + 4:int(b[2] * w) - 4]
    assert region(blurred, B1).std() < 0.25 * region(orig, B1).std()    # bystander blurred
    assert region(blurred, B2).std() < 0.25 * region(orig, B2).std()
    assert region(blurred, B0).std() > 0.7 * region(orig, B0).std()     # matched face left clear
    upd = env["fam"].get(f"/api/mp/my/cases/{case['id']}/updates").json()["updates"]
    assert len(upd) == 1 and env["fam"].get(upd[0]["face"]).status_code == 200
    assert env["fam2"].get(f"/api/mp/files/sighting/{sid}").status_code == 404


def test_reject_drops_face_embedding_and_close_and_retention(env):
    mp = env["mp"]
    a, b = identity(4), identity(5)
    case_a, case_b = new_case(env["fam"], [a], "A"), new_case(env["fam"], [b], "B")
    verify(env["admin"], case_a); verify(env["admin"], case_b)
    faces = [(B0, near(a, seed=1), 90, 0.9), (B1, near(b, seed=2), 90, 0.9), (B2, identity(77), 90, 0.9)]
    sid = submit_sighting(env["public"], group_photo(faces)).json()["id"]
    q = {m["case_id"]: m for m in env["rev"].get("/api/mp/review/queue").json()["matches"]}
    assert set(q) == {case_a["id"], case_b["id"]}
    assert sum(f["embedding"] is not None for f in faces_of(mp, sid)) == 2   # bystander dropped
    env["rev"].post(f"/api/mp/review/matches/{q[case_b['id']]['id']}", data={"decision": "reject"})
    live = [f for f in faces_of(mp, sid) if f["embedding"] is not None]
    assert len(live) == 1 and json.loads(live[0]["bbox"]) == list(B0)
    env["fam"].post(f"/api/mp/my/cases/{case_a['id']}/close", data={"reason": "found"})
    assert all(f["embedding"] is None for f in faces_of(mp, sid))
    # retention removes the photo and every face row
    mp.S.x("UPDATE sightings SET created_at=created_at-40*86400 WHERE id=?", (sid,))
    mp.S.x("UPDATE matches SET status='rejected' WHERE sighting_id=?", (sid,))
    env["admin"].post("/api/mp/admin/purge")
    assert faces_of(mp, sid) == [] and mp.S.one("SELECT id FROM sightings WHERE id=?", (sid,)) is None


def test_exif_gps_stripped_from_stored_photo(env):
    mp = env["mp"]
    im = Image.new("RGB", (200, 160), (90, 120, 140))
    exif = Image.Exif(); exif[0x8825] = {2: (47.0, 36.0, 22.0), 1: "N"}
    buf = io.BytesIO(); im.save(buf, "JPEG", exif=exif); data = buf.getvalue()
    import hashlib
    from mp_common import REGISTRY
    REGISTRY[hashlib.sha256(data).hexdigest()] = [{"bbox": [0.2, 0.2, 0.6, 0.7], "embedding": identity(6), "size": 80, "det_score": 0.9}]
    sid = env["public"].post("/api/mp/sightings", files={"file": ("e.jpg", data, "image/jpeg")},
                             data={"consent": "true", **__import__("mp_common").spam_fields(env["public"])}).json()["id"]
    f = mp.S.one("SELECT image_file FROM sightings WHERE id=?", (sid,))["image_file"]
    stored = Image.open(f"{mp.S.file_dir}/{f}")
    assert not stored.getexif().get_ifd(0x8825)
