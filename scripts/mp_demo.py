"""Run a throw-away missing-persons demo with synthetic data (no real faces, no models needed).

    python scripts/mp_demo.py --port 8123

Uses a temporary data dir, replaces the embedding step with a mock, seeds users/cases/sightings and serves the
app. Logins: admin / demo-admin-pass, reviewer1 / demo-review-pass, family1 / demo-family-pass.
"""
import argparse
import hashlib
import io
import os
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = tempfile.mkdtemp(prefix="mp-demo-")
os.environ.update(FACEID_DATA_DIR=DATA, FACEID_PASSWORD="", FACEID_LAZY_MODELS="1",
                  FACEID_MP_ADMIN_PASSWORD="demo-admin-pass")

REG = {}


def unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def silhouette(seed, hue):
    """Synthetic portrait placeholder: a stylised head-and-shoulders silhouette, not a real person."""
    rng = np.random.default_rng(seed)
    w, h = 360, 420
    bg = tuple(int(c) for c in rng.integers(18, 60, 3))
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    for i in range(0, h, 6):
        d.line([(0, i), (w, i)], fill=tuple(min(255, c + 6) for c in bg))
    col = hue
    cx = w // 2 + int(rng.integers(-25, 25))
    d.ellipse([cx - 150, 280, cx + 150, 560], fill=col)
    d.ellipse([cx - 72, 90, cx + 72, 270], fill=col)
    d.rectangle([cx - 30, 240, cx + 30, 300], fill=col)
    hair = tuple(max(0, c - 70) for c in col)
    d.chord([cx - 78, 78, cx + 78, 210], 180, 360, fill=hair)
    return im.filter(ImageFilter.GaussianBlur(1.2))


def group_scene(seed, hues, w=1200, h=640):
    """Synthetic crowd: several silhouettes side by side. Returns (image, [head bbox fractions])."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", (w, h), (24, 34, 44))
    d = ImageDraw.Draw(im)
    for i in range(0, h, 6):
        d.line([(0, i), (w, i)], fill=(30, 42, 54))
    d.rectangle([0, int(h * .78), w, h], fill=(36, 48, 58))
    boxes = []
    n = len(hues)
    for k, col in enumerate(hues):
        cx = int((k + .5) * w / n + rng.integers(-20, 20))
        s = float(rng.uniform(.8, 1.05))
        top = int(h * .18 + rng.integers(-20, 30))
        hw, hh = int(62 * s), int(80 * s)
        d.ellipse([cx - int(140 * s), top + int(150 * s), cx + int(140 * s), top + int(420 * s)], fill=col)
        d.rectangle([cx - int(28 * s), top + int(120 * s), cx + int(28 * s), top + int(180 * s)], fill=col)
        d.ellipse([cx - hw, top, cx + hw, top + 2 * hh], fill=col)
        d.chord([cx - hw - 6, top - 10, cx + hw + 6, top + int(1.2 * hh)], 180, 360, fill=tuple(max(0, c - 70) for c in col))
        boxes.append([(cx - hw - 8) / w, (top - 12) / h, (cx + hw + 8) / w, (top + 2 * hh + 8) / h])
    return im.filter(ImageFilter.GaussianBlur(1.0)), boxes


def jpg(im):
    b = io.BytesIO(); im.save(b, "JPEG", quality=88); return b.getvalue()


def reg(data, vec):
    REG[hashlib.sha256(data).hexdigest()] = unit(vec)
    return data


def reg_group(data, boxes, vecs):
    REG[hashlib.sha256(data).hexdigest()] = [{"bbox": b, "embedding": unit(v), "size": 120, "det_score": 0.9}
                                             for b, v in zip(boxes, vecs)]
    return data


def solve_pow(token, bits):
    i = 0
    while bits and int.from_bytes(hashlib.sha256(f"{token}:{i}".encode()).digest(), "big") >> (256 - bits):
        i += 1
    return str(i) if bits else ""


def spam(c, form="sighting"):
    t = c.get(f"/api/mp/form-token?form={form}").json()
    return {"form_token": t["token"], "pow_nonce": solve_pow(t["token"], t["pow_bits"])}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8123); a = ap.parse_args()
    if a.port in (8000, 8010):
        raise SystemExit("use another port")
    import app.main as m
    import app.missing as mp
    from app.missing import NoFace

    def mock(data):
        e = REG.get(hashlib.sha256(data).hexdigest())
        if e is None:  # uploads from the UI: give each a fresh random identity
            e = unit(np.random.default_rng(int(hashlib.sha256(data).hexdigest()[:8], 16)).normal(size=512))
        return e
    mp.embedder = mock

    from fastapi.testclient import TestClient
    adm = TestClient(m.app); adm.post("/api/mp/login", data={"username": "admin", "password": "demo-admin-pass"})
    adm.post("/api/mp/admin/users", data={"username": "reviewer1", "password": "demo-review-pass", "role": "reviewer"})
    adm.post("/api/mp/admin/users", data={"username": "family1", "password": "demo-family-pass", "role": "family"})
    fam = TestClient(m.app); fam.post("/api/mp/login", data={"username": "family1", "password": "demo-family-pass"})
    rng = np.random.default_rng(1)
    people = {"Jordan Reyes": ((70, 170, 200), unit(rng.normal(size=512))),
              "Sam Okafor": ((60, 200, 170), unit(rng.normal(size=512))),
              "Lee Marsh": ((90, 150, 210), unit(rng.normal(size=512)))}
    cases = {}
    for i, (name, (hue, v)) in enumerate(people.items()):
        files = [("files", (f"{k}.jpg", reg(jpg(silhouette(i * 10 + k, hue)), v + 0.2 * unit(rng.normal(size=512))), "image/jpeg"))
                 for k in range(2)]
        cases[name] = fam.post("/api/mp/cases", data={"person_name": name, "last_seen_text": ["Union Station, LA", "Pike Place, Seattle", "Old Town, Portland"][i],
                                                      "last_seen_at": "2026-11-0" + str(i + 3), "description": "Synthetic demo case"},
                               files=files).json()["case"]
    for name in ("Jordan Reyes", "Sam Okafor"):  # Lee Marsh stays pending until the group-photo step
        adm.post(f"/api/mp/admin/cases/{cases[name]['id']}/verify", data={"verification_ref": f"PD-2026-{cases[name]['id']:05d}"})
    g = mp.GUARD
    saved = (g.cfg.min_fill, g.cfg.rl_ip_max)
    g.cfg.min_fill, g.cfg.rl_ip_max = 0, 1000   # seeding only; restored before serving
    pub = TestClient(m.app)
    spots = [("Jordan Reyes", 0.55, "7th St Metro, Los Angeles", 34.0486, -118.2589),
             ("Sam Okafor", 0.75, "Westlake Center, Seattle", 47.6114, -122.3374),
             ("Jordan Reyes", 0.9, "Grand Park, Los Angeles", 34.0562, -118.2468)]
    for j, (name, noise, where, lat, lon) in enumerate(spots):
        hue, v = people[name]
        img = reg(jpg(silhouette(100 + j, hue)), v + noise * unit(rng.normal(size=512)))
        pub.post("/api/mp/sightings", files={"file": ("s.jpg", img, "image/jpeg")},
                 data={"consent": "true", "location_text": where, "lat": lat, "lon": lon, "seen_at": "2026-11-19 18:2" + str(j),
                       "note": "Wearing a grey hoodie" if j == 0 else "", **spam(pub)})
    # a synthetic group photo: Lee Marsh's case is verified now; face 3 of 4 is the match
    adm.post(f"/api/mp/admin/cases/{cases['Lee Marsh']['id']}/verify", data={"verification_ref": "PD-2026-00003"})
    hue_l, v_l = people["Lee Marsh"]
    scene, boxes = group_scene(7, [(200, 140, 110), (120, 170, 120), hue_l, (180, 160, 90)])
    vecs = [unit(rng.normal(size=512)), unit(rng.normal(size=512)), v_l + 0.45 * unit(rng.normal(size=512)),
            unit(rng.normal(size=512))]
    data = reg_group(jpg(scene), boxes, vecs)
    pub.post("/api/mp/sightings", files={"file": ("group.jpg", data, "image/jpeg")},
             data={"consent": "true", "location_text": "Saturday Market, Portland", "lat": 45.5226, "lon": -122.6703,
                   "seen_at": "2026-11-19 14:05", "note": "Group near the fountain", **spam(pub)})
    g.cfg.min_fill, g.cfg.rl_ip_max = saved
    print("demo data dir:", DATA, flush=True)
    import uvicorn
    uvicorn.run(m.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
