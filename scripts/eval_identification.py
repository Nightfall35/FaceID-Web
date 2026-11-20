"""Open-set identification benchmark on LFW, mirroring how the app is used.

* Gallery: the first K photos of each of N people with >= K+2 photos ("enrolled").
* Known probes: up to P further photos of each enrolled person.
* Unknown probes: one photo each of M people who are NOT enrolled.
Reports, per threshold: correct-identification rate on known probes (DIR), how often
a known probe is given the wrong name, and how often an unknown is falsely labeled
(FPIR). Uses the app's own Gallery and open-set rule (max-similarity + threshold + margin).

    python scripts/eval_identification.py --lfw-dir data/lfw/lfw
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.face_engine import FaceEngine, load_image  # noqa: E402
from app.matcher import Gallery  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--lfw-dir", default="data/lfw/lfw")
ap.add_argument("--people", type=int, default=100)
ap.add_argument("--k", type=int, default=3, help="enrollment photos per person")
ap.add_argument("--probes", type=int, default=5)
ap.add_argument("--unknown", type=int, default=600)
ap.add_argument("--out", default="eval_identification.json")
a = ap.parse_args()

rng = np.random.default_rng(42)
names = sorted(os.listdir(a.lfw_dir))
counts = {n: len(os.listdir(os.path.join(a.lfw_dir, n))) for n in names}
eligible = [n for n in names if counts[n] >= a.k + 2]
enrolled = sorted(rng.choice(eligible, size=min(a.people, len(eligible)), replace=False))
others = [n for n in names if n not in set(enrolled)]
unknown = sorted(rng.choice(others, size=min(a.unknown, len(others)), replace=False))
eng = FaceEngine()


def emb(name, i):
    img = load_image(open(os.path.join(a.lfw_dir, name, f"{name}_{i:04d}.jpg"), "rb").read())
    faces = eng.analyze(img) or eng.analyze(img, det_thresh=0.1)
    if not faces:
        return None
    h, w = img.shape[:2]
    c = np.array([w / 2, h / 2])
    return min(faces, key=lambda f: np.linalg.norm((f.bbox[:2] + f.bbox[2:]) / 2 - c)).embedding


entries, probes = [], []
for pid, n in enumerate(enrolled):
    for i in range(1, a.k + 1):
        e = emb(n, i)
        if e is not None:
            entries.append((pid, n, e))
    for i in range(a.k + 1, min(counts[n], a.k + a.probes) + 1):
        e = emb(n, i)
        if e is not None:
            probes.append((pid, e))
unk = [e for e in (emb(n, 1) for n in unknown) if e is not None]
g = Gallery(entries)
print(f"enrolled={len(enrolled)} gallery_photos={len(entries)} known_probes={len(probes)} unknown_probes={len(unk)}")

Q = np.stack([e for _, e in probes])
truth = np.array([p for p, _ in probes])
U = np.stack(unk)
comb_q, _, _ = g.person_scores(Q)
rank1 = float((np.array(g.person_ids)[comb_q.argmax(1)] == truth).mean())
rows = []
for margin in (0.0, 0.05):
    for t in np.round(np.arange(0.30, 0.61, 0.05), 2):
        mq = g.identify(Q, threshold=float(t), unique=False, margin=margin)
        mu = g.identify(U, threshold=float(t), unique=False, margin=margin)
        pred = np.array([-1 if m.person_id is None else m.person_id for m in mq])
        correct = float((pred == truth).mean())
        wrong = float(((pred != -1) & (pred != truth)).mean())
        rows.append({"threshold": float(t), "margin": margin, "known_correct": correct, "known_wrong_name": wrong,
                     "known_rejected": float((pred == -1).mean()),
                     "unknown_false_accept": float(np.mean([m.person_id is not None for m in mu]))})
res = {"setup": vars(a) | {"gallery_photos": len(entries), "known_probes": len(probes), "unknown_probes": len(unk)},
       "closed_set_rank1": rank1, "by_threshold": rows}
print(json.dumps(res, indent=2))
json.dump(res, open(a.out, "w"), indent=2)
