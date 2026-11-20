"""Face detection (SCRFD-10G) + recognition (ArcFace R50, WebFace600K) via ONNX Runtime.

These are the same models InsightFace ships as its `buffalo_l` pack; we run them
directly with onnxruntime so no compiled insightface wheel is needed.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np
import onnxruntime as ort

MODEL_DIR = os.environ.get("FACEID_MODEL_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "models"))

# Canonical ArcFace 5-point template for a 112x112 crop.
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class Face:
    bbox: np.ndarray  # x1, y1, x2, y2
    kps: np.ndarray  # 5x2
    det_score: float
    embedding: np.ndarray | None = None  # L2-normalised 512-d
    quality: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bbox": [round(float(v), 1) for v in self.bbox],
            "kps": [[round(float(x), 1), round(float(y), 1)] for x, y in self.kps],
            "det_score": round(float(self.det_score), 4),
            "quality": self.quality,
        }


def _session(path: str) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = int(os.environ.get("FACEID_THREADS", "0")) or max(1, (os.cpu_count() or 2))
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


def _nms(dets: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


class SCRFD:
    def __init__(self, path: str, input_size=(640, 640)):
        self.sess = _session(path)
        self.input_name = self.sess.get_inputs()[0].name
        self.output_names = [o.name for o in self.sess.get_outputs()]
        self.input_size = input_size
        n = len(self.output_names)
        assert n == 9, f"unexpected SCRFD outputs: {n}"
        self.fmc, self.strides, self.num_anchors = 3, [8, 16, 32], 2
        self._anchor_cache: dict = {}

    def _anchors(self, h, w, stride):
        key = (h, w, stride)
        if key not in self._anchor_cache:
            c = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
            c = (c * stride).reshape(-1, 2)
            c = np.stack([c] * self.num_anchors, axis=1).reshape(-1, 2)
            self._anchor_cache[key] = c
        return self._anchor_cache[key]

    def detect(self, img_bgr: np.ndarray, thresh=0.5, nms_thresh=0.4, max_side=None):
        iw, ih = self.input_size
        h, w = img_bgr.shape[:2]
        scale = min(iw / w, ih / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(img_bgr, (nw, nh))
        canvas = np.zeros((ih, iw, 3), dtype=np.uint8)
        canvas[:nh, :nw] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (iw, ih), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run(self.output_names, {self.input_name: blob})
        scores_l, boxes_l, kps_l = [], [], []
        for idx, stride in enumerate(self.strides):
            scores = outs[idx].reshape(-1)
            bbox = outs[idx + self.fmc].reshape(-1, 4) * stride
            kps = outs[idx + self.fmc * 2].reshape(-1, 10) * stride
            anchors = self._anchors(ih // stride, iw // stride, stride)
            pos = np.where(scores >= thresh)[0]
            if pos.size == 0:
                continue
            a = anchors[pos]
            b = bbox[pos]
            boxes = np.stack([a[:, 0] - b[:, 0], a[:, 1] - b[:, 1], a[:, 0] + b[:, 2], a[:, 1] + b[:, 3]], axis=-1)
            k = kps[pos].reshape(-1, 5, 2) + a[:, None, :]
            scores_l.append(scores[pos])
            boxes_l.append(boxes)
            kps_l.append(k)
        if not scores_l:
            return []
        scores = np.concatenate(scores_l)
        boxes = np.concatenate(boxes_l) / scale
        kpss = np.concatenate(kps_l) / scale
        dets = np.hstack([boxes, scores[:, None]]).astype(np.float32)
        keep = _nms(dets, nms_thresh)
        faces = []
        for i in keep:
            bx = dets[i, :4].copy()
            bx[[0, 2]] = np.clip(bx[[0, 2]], 0, w)
            bx[[1, 3]] = np.clip(bx[[1, 3]], 0, h)
            faces.append(Face(bbox=bx, kps=kpss[i], det_score=float(dets[i, 4])))
        faces.sort(key=lambda f: -f.det_score)
        return faces


def align_face(img_bgr: np.ndarray, kps: np.ndarray, size=112) -> np.ndarray:
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST * (size / 112.0), method=cv2.LMEDS)
    return cv2.warpAffine(img_bgr, M, (size, size), borderValue=0.0)


class ArcFace:
    def __init__(self, path: str):
        self.sess = _session(path)
        self.input_name = self.sess.get_inputs()[0].name
        self.output_name = self.sess.get_outputs()[0].name

    def embed(self, crops_bgr: list[np.ndarray], flip_tta=True) -> np.ndarray:
        if not crops_bgr:
            return np.zeros((0, 512), np.float32)
        imgs = list(crops_bgr)
        if flip_tta:
            imgs += [cv2.flip(c, 1) for c in crops_bgr]
        blob = cv2.dnn.blobFromImages(imgs, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        feats = self.sess.run([self.output_name], {self.input_name: blob})[0]
        if flip_tta:
            n = len(crops_bgr)
            feats = feats[:n] + feats[n:]
        return feats / np.linalg.norm(feats, axis=1, keepdims=True)


def blur_score(crop_bgr: np.ndarray) -> float:
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class FaceEngine:
    """Thread-safe wrapper: detect -> align -> embed."""

    def __init__(self, model_dir: str = MODEL_DIR):
        self.detector = SCRFD(os.path.join(model_dir, "det_10g.onnx"))
        self.recognizer = ArcFace(os.path.join(model_dir, "w600k_r50.onnx"))
        self._lock = threading.Lock()

    def analyze(self, img_bgr: np.ndarray, det_thresh=0.5, max_faces=64) -> list[Face]:
        with self._lock:
            faces = self.detector.detect(img_bgr, thresh=det_thresh)[:max_faces]
            if not faces:
                return []
            crops = [align_face(img_bgr, f.kps) for f in faces]
            embs = self.recognizer.embed(crops)
        for f, c, e in zip(faces, crops, embs):
            f.embedding = e.astype(np.float32)
            bw, bh = f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]
            f.quality = {
                "face_size": int(min(bw, bh)),
                "blur": round(blur_score(c), 1),
                "det_score": round(float(f.det_score), 3),
            }
        return faces


def load_image(data: bytes, max_side=2000) -> np.ndarray:
    """Decode bytes into BGR, honouring EXIF orientation, and cap the size."""
    from io import BytesIO

    from PIL import Image, ImageOps

    try:
        im = Image.open(BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Could not read image: {e}") from e
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
