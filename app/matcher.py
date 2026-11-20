"""Pure-numpy open-set identification over a gallery of enrolled embeddings.

Scoring per enrolled person combines
  * nearest neighbour: max cosine similarity to any of their reference photos, and
  * centroid: cosine similarity to the L2-normalised mean of their embeddings,
which is more robust than either alone once a person has several photos.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_THRESHOLD = 0.40  # cosine; see README (chosen from LFW / identification eval)
NN_WEIGHT = 0.6


def l2n(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def confidence_from_score(score: float, threshold: float) -> float:
    """Map cosine score to a 0..1 confidence: 0.5 at the threshold, ~0.95 at +0.2 above it."""
    return float(1.0 / (1.0 + np.exp(-15.0 * (score - threshold))))


@dataclass
class Match:
    person_id: int | None
    name: str | None
    score: float  # combined cosine score for the best person
    nn_score: float
    centroid_score: float
    confidence: float
    runner_up: str | None = None
    runner_up_score: float | None = None
    ambiguous: bool = False

    def to_dict(self):
        return {
            "person_id": self.person_id,
            "name": self.name if self.person_id is not None else "Unknown",
            "known": self.person_id is not None,
            "score": round(self.score, 4),
            "nn_score": round(self.nn_score, 4),
            "centroid_score": round(self.centroid_score, 4),
            "confidence": round(self.confidence, 4),
            "runner_up": self.runner_up,
            "runner_up_score": None if self.runner_up_score is None else round(self.runner_up_score, 4),
            "ambiguous": self.ambiguous,
        }


class Gallery:
    def __init__(self, entries: list[tuple[int, str, np.ndarray]] | None = None):
        """entries: (person_id, person_name, embedding)"""
        self.person_ids: list[int] = []
        self.names: dict[int, str] = {}
        self._emb = np.zeros((0, 512), np.float32)
        self._owner = np.zeros((0,), np.int64)
        self._centroids = np.zeros((0, 512), np.float32)
        if entries:
            self.build(entries)

    def build(self, entries):
        self.names = {pid: name for pid, name, _ in entries}
        self.person_ids = sorted(self.names)
        if not entries:
            self.__init__()
            return
        index = {pid: i for i, pid in enumerate(self.person_ids)}
        self._emb = l2n(np.stack([e for _, _, e in entries]))
        self._owner = np.array([index[pid] for pid, _, _ in entries], np.int64)
        cents = np.zeros((len(self.person_ids), self._emb.shape[1]), np.float32)
        np.add.at(cents, self._owner, self._emb)
        self._centroids = l2n(cents)

    def __len__(self):
        return len(self.person_ids)

    def person_scores(self, queries: np.ndarray):
        """Return (combined, nn, centroid) arrays of shape [n_queries, n_people]."""
        q = l2n(np.atleast_2d(queries))
        P = len(self.person_ids)
        if P == 0:
            z = np.zeros((q.shape[0], 0), np.float32)
            return z, z, z
        sims = q @ self._emb.T  # [Q, N]
        nn = np.full((q.shape[0], P), -1.0, np.float32)
        for j in range(P):
            nn[:, j] = sims[:, self._owner == j].max(axis=1)
        cen = q @ self._centroids.T
        counts = np.bincount(self._owner, minlength=P)
        w = np.where(counts > 1, NN_WEIGHT, 1.0)[None, :]
        combined = w * nn + (1 - w) * cen
        return combined, nn, cen

    def identify(self, queries: np.ndarray, threshold: float = DEFAULT_THRESHOLD, unique: bool = True,
                 ambiguity_margin: float = 0.05) -> list[Match]:
        """Identify each query face. If `unique`, a person may be assigned to at most one
        face per image (greedy best-first assignment), which removes duplicate labels."""
        q = np.atleast_2d(queries)
        n = q.shape[0]
        if n == 0:
            return []
        combined, nn, cen = self.person_scores(q)
        P = combined.shape[1]
        assignment: dict[int, int | None] = {i: None for i in range(n)}
        if P:
            if unique:
                order = np.dstack(np.unravel_index(np.argsort(-combined, axis=None), combined.shape))[0]
                used_faces, used_people = set(), set()
                for i, j in order:
                    if combined[i, j] < threshold:
                        break
                    if i in used_faces or j in used_people:
                        continue
                    assignment[int(i)] = int(j)
                    used_faces.add(i)
                    used_people.add(j)
            else:
                for i in range(n):
                    j = int(np.argmax(combined[i]))
                    if combined[i, j] >= threshold:
                        assignment[i] = j
        out = []
        for i in range(n):
            row = combined[i] if P else np.array([])
            ranked = np.argsort(-row) if P else []
            j = assignment[i]
            if j is None:
                best = int(ranked[0]) if P else None
                s = float(row[best]) if P else 0.0
                out.append(Match(None, None, s, float(nn[i, best]) if P else 0.0, float(cen[i, best]) if P else 0.0,
                                 1.0 - confidence_from_score(s, threshold),
                                 runner_up=self.names[self.person_ids[best]] if P else None,
                                 runner_up_score=s if P else None))
                continue
            s = float(row[j])
            others = [k for k in ranked if k != j]
            ru = int(others[0]) if others else None
            ru_s = float(row[ru]) if ru is not None else None
            out.append(Match(
                person_id=self.person_ids[j], name=self.names[self.person_ids[j]], score=s,
                nn_score=float(nn[i, j]), centroid_score=float(cen[i, j]),
                confidence=confidence_from_score(s, threshold),
                runner_up=self.names[self.person_ids[ru]] if ru is not None else None,
                runner_up_score=ru_s,
                ambiguous=bool(ru_s is not None and ru_s >= threshold and s - ru_s < ambiguity_margin),
            ))
        return out
