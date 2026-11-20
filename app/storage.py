"""SQLite + filesystem persistence for people, reference photos and settings."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notes TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    image_file TEXT NOT NULL,
    thumb_file TEXT NOT NULL,
    embedding BLOB NOT NULL,
    bbox TEXT NOT NULL,
    quality TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.photo_dir = os.path.join(self.data_dir, "photos")
        os.makedirs(self.photo_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "faceid.sqlite3")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _one(self, sql: str, params=()):
        # The connection is shared across FastAPI's threadpool; serialise reads too, otherwise
        # concurrent requests (e.g. several thumbnails loading at once) intermittently fail with
        # sqlite3.InterfaceError / missing rows.
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- people ----
    def create_person(self, name: str, notes: str = "") -> dict:
        with self._lock:
            cur = self._conn.execute("INSERT INTO people (name, notes, created_at) VALUES (?, ?, ?)",
                                     (name, notes, time.time()))
            self._conn.commit()
            return self.get_person(cur.lastrowid)

    def get_person(self, pid: int) -> dict | None:
        r = self._one("SELECT * FROM people WHERE id=?", (pid,))
        return dict(r) if r else None

    def rename_person(self, pid: int, name: str):
        with self._lock:
            self._conn.execute("UPDATE people SET name=? WHERE id=?", (name, pid))
            self._conn.commit()

    def list_people(self) -> list[dict]:
        rows = self._all(
            "SELECT p.*, COUNT(ph.id) AS photo_count FROM people p LEFT JOIN photos ph ON ph.person_id=p.id "
            "GROUP BY p.id ORDER BY p.name COLLATE NOCASE")
        out = []
        for r in rows:
            d = dict(r)
            d["photos"] = self.list_photos(d["id"])
            out.append(d)
        return out

    def delete_person(self, pid: int):
        with self._lock:
            for ph in self.list_photos(pid):
                self._remove_files(ph)
            self._conn.execute("DELETE FROM people WHERE id=?", (pid,))
            self._conn.commit()

    # ---- photos ----
    def add_photo(self, person_id: int, image_jpg: bytes, thumb_jpg: bytes, embedding: np.ndarray,
                  bbox, quality: dict) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO photos (person_id, image_file, thumb_file, embedding, bbox, quality, created_at) "
                "VALUES (?, '', '', ?, ?, ?, ?)",
                (person_id, embedding.astype(np.float32).tobytes(), json.dumps(bbox), json.dumps(quality), time.time()))
            phid = cur.lastrowid
            img_name, th_name = f"{phid}.jpg", f"{phid}_thumb.jpg"
            with open(os.path.join(self.photo_dir, img_name), "wb") as f:
                f.write(image_jpg)
            with open(os.path.join(self.photo_dir, th_name), "wb") as f:
                f.write(thumb_jpg)
            self._conn.execute("UPDATE photos SET image_file=?, thumb_file=? WHERE id=?", (img_name, th_name, phid))
            self._conn.commit()
            return self.get_photo(phid)

    def _photo_dict(self, r) -> dict:
        d = dict(r)
        d.pop("embedding", None)
        d["bbox"] = json.loads(d["bbox"])
        d["quality"] = json.loads(d["quality"])
        return d

    def get_photo(self, phid: int) -> dict | None:
        r = self._one("SELECT * FROM photos WHERE id=?", (phid,))
        return self._photo_dict(r) if r else None

    def list_photos(self, pid: int) -> list[dict]:
        rows = self._all("SELECT * FROM photos WHERE person_id=? ORDER BY id", (pid,))
        return [self._photo_dict(r) for r in rows]

    def person_embeddings(self, pid: int) -> np.ndarray:
        rows = self._all("SELECT embedding FROM photos WHERE person_id=?", (pid,))
        if not rows:
            return np.zeros((0, 512), np.float32)
        return np.stack([np.frombuffer(r[0], np.float32) for r in rows])

    def all_embeddings(self) -> list[tuple[int, str, np.ndarray]]:
        rows = self._all(
            "SELECT ph.person_id, p.name, ph.embedding FROM photos ph JOIN people p ON p.id=ph.person_id")
        return [(r[0], r[1], np.frombuffer(r[2], np.float32).copy()) for r in rows]

    def _remove_files(self, ph: dict):
        for k in ("image_file", "thumb_file"):
            if ph.get(k):
                try:
                    os.remove(os.path.join(self.photo_dir, ph[k]))
                except FileNotFoundError:
                    pass

    def delete_photo(self, phid: int):
        with self._lock:
            ph = self.get_photo(phid)
            if ph:
                self._remove_files(ph)
                self._conn.execute("DELETE FROM photos WHERE id=?", (phid,))
                self._conn.commit()
            return ph

    # ---- settings ----
    def get_setting(self, key: str, default=None):
        r = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]) if r else default

    def set_setting(self, key: str, value):
        with self._lock:
            self._conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))
            self._conn.commit()

    def wipe(self):
        with self._lock:
            self._conn.execute("DELETE FROM photos")
            self._conn.execute("DELETE FROM people")
            self._conn.execute("DELETE FROM settings")
            self._conn.commit()
            self._conn.execute("VACUUM")
            shutil.rmtree(self.photo_dir, ignore_errors=True)
            os.makedirs(self.photo_dir, exist_ok=True)

    def stats(self) -> dict:
        p = self._one("SELECT COUNT(*) FROM people")[0]
        ph = self._one("SELECT COUNT(*) FROM photos")[0]
        return {"people": p, "photos": ph}
