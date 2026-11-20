"""End-to-end API tests using real LFW photos (skipped if the dataset isn't available)."""
import io
import os

import cv2
import numpy as np
import pytest

LFW = os.environ.get("LFW_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "lfw", "lfw"))
if not os.path.isdir(LFW) and os.path.isdir("/workspace/lfw_cache/lfw"):
    LFW = "/workspace/lfw_cache/lfw"
pytestmark = pytest.mark.skipif(not os.path.isdir(LFW), reason="LFW images not available")


def img(name, i):
    return open(os.path.join(LFW, name, f"{name}_{i:04d}.jpg"), "rb").read()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["FACEID_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    os.environ["FACEID_PASSWORD"] = "testpw"
    import importlib

    import app.main as m
    importlib.reload(m)
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    yield c


def test_auth_required(client):
    assert client.get("/api/people").status_code == 401
    assert client.post("/api/login", data={"password": "nope"}).status_code == 401
    assert client.post("/api/login", data={"password": "testpw"}).status_code == 200
    assert client.get("/api/people").status_code == 200


def test_enroll_and_identify(client):
    ids = {}
    for name in ("Colin_Powell", "Serena_Williams"):
        pid = client.post("/api/people", data={"name": name}).json()["id"]
        ids[name] = pid
        files = [("files", (f"{i}.jpg", img(name, i), "image/jpeg")) for i in (1, 2, 3)]
        r = client.post(f"/api/people/{pid}/photos", files=files).json()
        assert all(x["accepted"] for x in r["results"]), r
    assert client.post("/api/people", data={"name": "colin_powell"}).status_code == 409

    files = [("files", ("cp.jpg", img("Colin_Powell", 10), "image/jpeg")),
             ("files", ("sw.jpg", img("Serena_Williams", 10), "image/jpeg")),
             ("files", ("unk.jpg", img("Tony_Blair", 1), "image/jpeg"))]
    r = client.post("/api/identify", files=files).json()
    names = [res["faces"][0]["name"] for res in r["results"]]
    assert names == ["Colin_Powell", "Serena_Williams", "Unknown"]
    assert r["results"][0]["faces"][0]["confidence"] > 0.8


def test_rejects_bad_enrollment_photos(client):
    pid = client.post("/api/people", data={"name": "Bad Photos"}).json()["id"]
    blank = cv2.imencode(".jpg", np.full((300, 300, 3), 127, np.uint8))[1].tobytes()
    face = cv2.imdecode(np.frombuffer(img("George_W_Bush", 1), np.uint8), cv2.IMREAD_COLOR)
    blurry = cv2.imencode(".jpg", cv2.GaussianBlur(face, (0, 0), 6))[1].tobytes()
    tiny = cv2.imencode(".jpg", cv2.resize(face, (60, 60)))[1].tobytes()
    files = [("files", ("blank.jpg", blank, "image/jpeg")), ("files", ("blurry.jpg", blurry, "image/jpeg")),
             ("files", ("tiny.jpg", tiny, "image/jpeg")), ("files", ("junk.jpg", b"not an image", "image/jpeg"))]
    res = client.post(f"/api/people/{pid}/photos", files=files).json()["results"]
    assert not any(r["accepted"] for r in res)
    assert "No face" in res[0]["errors"][0]
    assert any("blurry" in e or "visible" in e for e in res[1]["errors"]), res[1]
    assert res[2]["errors"]


def test_inconsistent_photo_blocked_unless_forced(client):
    people = client.get("/api/people").json()["people"]
    pid = next(p["id"] for p in people if p["name"] == "Colin_Powell")
    f = [("files", ("x.jpg", img("Gloria_Macapagal_Arroyo", 1), "image/jpeg"))]
    r = client.post(f"/api/people/{pid}/photos", files=f).json()["results"][0]
    assert not r["accepted"] and "look like" in r["errors"][0]


def test_threshold_setting_and_delete(client):
    assert client.post("/api/settings", data={"threshold": 0.5}).json()["threshold"] == 0.5
    people = [p for p in client.get("/api/people").json()["people"] if p["photos"]]
    ph = people[0]["photos"][0]["id"]
    assert client.get(f"/api/photos/{ph}/thumb").status_code == 200
    assert client.delete(f"/api/photos/{ph}").status_code == 200
    assert client.delete(f"/api/people/{people[0]['id']}").status_code == 200
    assert client.post("/api/wipe", data={"confirm": "nah"}).status_code == 400
    assert client.post("/api/wipe", data={"confirm": "DELETE"}).status_code == 200
    s = client.get("/api/settings").json()
    assert s["people"] == 0 and s["photos"] == 0 and s["threshold"] == 0.4
