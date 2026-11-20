"""Evaluate the FaceID pipeline on the LFW 10-fold verification protocol (6,000 pairs).

Usage:
    python scripts/eval_lfw.py --lfw-dir /path/to/lfw --pairs /path/to/pairs.txt
    python scripts/eval_lfw.py --download   # fetches lfw.tgz + pairs.txt into ./data/lfw

Uses the *unaligned original* LFW images, so the full pipeline (SCRFD detection ->
5-point alignment -> ArcFace embedding -> cosine similarity) is measured end to end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
import time
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.face_engine import FaceEngine, load_image  # noqa: E402

MIRRORS = {
    "lfw.tgz": ("055f7d9c632d7370e6fb4afc7468d40f970c34a80d4c6f50ffec63f5a8d536c0", [
        "https://huggingface.co/datasets/DerrickUnleashed/LFW/resolve/main/lfw.tgz",
        "https://ndownloader.figshare.com/files/5976018",
    ]),
    "pairs.txt": ("ea42330c62c92989f9d7c03237ed5d591365e89b3e649747777b70e692dc1592", [
        "https://huggingface.co/datasets/DerrickUnleashed/LFW/resolve/main/pairs.txt",
        "https://ndownloader.figshare.com/files/5976006",
    ]),
}


def download(dest: str):
    os.makedirs(dest, exist_ok=True)
    for name, (sha, urls) in MIRRORS.items():
        path = os.path.join(dest, name)
        if not os.path.exists(path):
            for u in urls:
                try:
                    print("downloading", u)
                    urllib.request.urlretrieve(u, path)
                    break
                except Exception as e:  # noqa: BLE001
                    print("  failed:", e)
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if digest != sha:
            raise SystemExit(f"checksum mismatch for {name}")
    if not os.path.isdir(os.path.join(dest, "lfw")):
        with tarfile.open(os.path.join(dest, "lfw.tgz")) as t:
            t.extractall(dest)
    return os.path.join(dest, "lfw"), os.path.join(dest, "pairs.txt")


def read_pairs(path):
    lines = open(path).read().strip().splitlines()
    n_folds, n = map(int, lines[0].split())
    pairs = []
    for ln in lines[1:]:
        p = ln.split("\t")
        if len(p) == 3:
            pairs.append(((p[0], int(p[1])), (p[0], int(p[2])), 1))
        else:
            pairs.append(((p[0], int(p[1])), (p[2], int(p[3])), 0))
    return pairs, n_folds, n


def img_path(root, name, idx):
    return os.path.join(root, name, f"{name}_{idx:04d}.jpg")


def pick_face(faces, shape):
    """LFW images are centred on the subject: choose the face closest to centre."""
    h, w = shape[:2]
    c = np.array([w / 2, h / 2])
    return min(faces, key=lambda f: np.linalg.norm((f.bbox[:2] + f.bbox[2:]) / 2 - c))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lfw-dir")
    ap.add_argument("--pairs")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--data-dir", default="data/lfw")
    ap.add_argument("--out", default="eval_results.json")
    a = ap.parse_args()
    if a.download or not a.lfw_dir:
        a.lfw_dir, a.pairs = download(a.data_dir)

    pairs, n_folds, n = read_pairs(a.pairs)
    eng = FaceEngine()
    keys = sorted({k for p in pairs for k in p[:2]})
    emb, missed = {}, 0
    t0 = time.time()
    for i, k in enumerate(keys):
        img = load_image(open(img_path(a.lfw_dir, *k), "rb").read())
        faces = eng.analyze(img, det_thresh=0.5)
        if not faces:
            faces = eng.analyze(img, det_thresh=0.1)  # fall back to a lower detector threshold
        if not faces:
            missed += 1
            emb[k] = None
            continue
        emb[k] = pick_face(faces, img.shape).embedding
        if i % 500 == 0:
            print(f"  {i}/{len(keys)} images  {time.time() - t0:.0f}s", flush=True)

    sims = np.array([float(emb[x] @ emb[y]) if emb[x] is not None and emb[y] is not None else -1.0 for x, y, _ in pairs])
    labels = np.array([p[2] for p in pairs])

    # Standard 10-fold protocol: choose threshold on 9 folds, test on the held-out fold.
    thresholds = np.arange(-0.2, 1.0, 0.0025)
    fold_acc, fold_thr = [], []
    fold_size = len(pairs) // n_folds
    for f in range(n_folds):
        test = np.zeros(len(pairs), bool)
        test[f * fold_size:(f + 1) * fold_size] = True
        accs = [((sims[~test] >= t) == labels[~test]).mean() for t in thresholds]
        t = thresholds[int(np.argmax(accs))]
        fold_thr.append(float(t))
        fold_acc.append(float(((sims[test] >= t) == labels[test]).mean()))

    def acc_at(t):
        return float(((sims >= t) == labels).mean())

    neg = np.sort(sims[labels == 0])[::-1]
    pos = sims[labels == 1]
    tar_at = {}
    for far in (0.01, 0.001):
        k = max(int(far * len(neg)), 1)
        thr = neg[k - 1] + 1e-6
        tar_at[f"TAR@FAR={far}"] = float((pos >= thr).mean())

    res = {
        "dataset": "LFW 10-fold (6000 pairs, original unaligned images)",
        "pipeline": "SCRFD-10G detect -> 5pt similarity align -> ArcFace R50 (WebFace600K) + flip TTA -> cosine",
        "images": len(keys),
        "images_no_face": missed,
        "accuracy_mean": float(np.mean(fold_acc)),
        "accuracy_std": float(np.std(fold_acc)),
        "best_thresholds_per_fold": fold_thr,
        "accuracy_at_0.30": acc_at(0.30),
        "accuracy_at_0.35": acc_at(0.35),
        "accuracy_at_0.40": acc_at(0.40),
        **tar_at,
        "seconds": round(time.time() - t0, 1),
    }
    print(json.dumps(res, indent=2))
    with open(a.out, "w") as fh:
        json.dump(res, fh, indent=2)


if __name__ == "__main__":
    main()
