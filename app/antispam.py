"""Self-hosted spam protection for the public missing-persons forms (no third-party service required).

Layers (each configurable, see docs/MISSING_PERSONS.md):
  * signed form token from GET /api/mp/form-token: proves the form was loaded, enforces a minimum fill time,
    single use (nonce stored in SQLite), max age
  * proof-of-work: sha256(token + ":" + nonce) must start with N zero bits (small JS hashcash in mp.js)
  * honeypot field `website` that humans never see
  * sliding-window rate limits per IP and globally (SQLite)
  * optional hCaptcha / Cloudflare Turnstile verification (off unless FACEID_MP_CAPTCHA is set)
  * upload checks: size, magic-byte type, dimensions, pixel count (decompression bombs)
  * duplicate-submission check by file hash / perceptual hash from the same IP

Every rejection is audited with its internal reason, but the HTTP response is always the same.
IPs are never stored raw: only an HMAC of the IP (keyed with the app secret).
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import warnings
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image, ImageOps

GENERIC = ("Your submission could not be accepted right now. Please make sure it is a normal JPEG, PNG or WebP "
           "photo, reload the page, wait a moment and try again.")
FORMS = ("sighting", "self")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_events (key TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rate_events ON rate_events(key, ts);
CREATE TABLE IF NOT EXISTS used_tokens (nonce TEXT PRIMARY KEY, ts REAL NOT NULL);
"""

_now = time.time  # patched in tests


def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _fl(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class SpamConfig:
    def __init__(self):
        self.enabled = os.environ.get("FACEID_MP_ANTISPAM", "1") != "0"
        self.min_fill = _fl("FACEID_MP_MIN_FILL_SECONDS", 3)
        self.token_max_age = _i("FACEID_MP_TOKEN_MAX_AGE", 7200)
        self.pow_bits = _i("FACEID_MP_POW_BITS", 14)              # 0 disables; ~16k hashes avg in the browser
        self.rl_window = _fl("FACEID_MP_RL_WINDOW_SECONDS", 600)
        self.rl_ip_max = _i("FACEID_MP_RL_IP_MAX", 10)             # per IP per window, per form
        self.rl_global_max = _i("FACEID_MP_RL_GLOBAL_MAX", 300)    # all IPs per window, per form
        self.trust_proxy = os.environ.get("FACEID_MP_TRUST_PROXY", "0") == "1"
        self.captcha = os.environ.get("FACEID_MP_CAPTCHA", "").strip().lower()   # '', 'hcaptcha', 'turnstile'
        self.captcha_sitekey = os.environ.get("FACEID_MP_CAPTCHA_SITEKEY", "").strip()
        self.captcha_secret = os.environ.get("FACEID_MP_CAPTCHA_SECRET", "").strip()
        self.max_upload = int(_fl("FACEID_MP_MAX_UPLOAD_MB", 10) * 1024 * 1024)
        self.max_side = _i("FACEID_MP_MAX_SIDE", 8000)
        self.min_side = _i("FACEID_MP_MIN_SIDE", 32)
        self.max_pixels = _i("FACEID_MP_MAX_PIXELS", 40_000_000)
        self.dup_hours = _fl("FACEID_MP_DUP_WINDOW_HOURS", 24)
        self.dup_hamming = _i("FACEID_MP_DUP_HAMMING", 4)


CAPTCHA_URLS = {"hcaptcha": "https://api.hcaptcha.com/siteverify",
                "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify"}
CAPTCHA_FIELDS = {"hcaptcha": "h-captcha-response", "turnstile": "cf-turnstile-response"}


class SpamGuard:
    def __init__(self, store, secret: str):
        self.S = store
        self.cfg = SpamConfig()
        self.secret = secret.encode()
        self.ser = URLSafeTimedSerializer(secret, salt="faceid-mp-form")
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ---- identity ----
    def client_ip(self, request: Request) -> str:
        if self.cfg.trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def ip_hash(self, request: Request) -> str:
        return hmac.new(self.secret, self.client_ip(request).encode(), hashlib.sha256).hexdigest()[:24]

    # ---- tokens / PoW ----
    def issue(self, form: str) -> dict:
        if form not in FORMS:
            raise HTTPException(400, "Unknown form")
        tok = self.ser.dumps({"f": form, "t": _now(), "n": secrets.token_hex(8)})
        out = {"token": tok, "pow_bits": self.cfg.pow_bits if self.cfg.enabled else 0,
               "min_fill_seconds": self.cfg.min_fill if self.cfg.enabled else 0, "captcha": None}
        if self.cfg.enabled and self.cfg.captcha in CAPTCHA_URLS and self.cfg.captcha_sitekey:
            out["captcha"] = {"provider": self.cfg.captcha, "sitekey": self.cfg.captcha_sitekey,
                              "field": CAPTCHA_FIELDS[self.cfg.captcha]}
        return out

    @staticmethod
    def pow_ok(token: str, nonce: str, bits: int) -> bool:
        if bits <= 0:
            return True
        if not nonce or len(nonce) > 32:
            return False
        h = int.from_bytes(hashlib.sha256(f"{token}:{nonce}".encode()).digest(), "big")
        return h >> (256 - bits) == 0

    def verify_captcha(self, response: str, ip: str) -> bool:  # patched in tests
        if not response or not self.cfg.captcha_secret:
            return False
        data = urllib.parse.urlencode({"secret": self.cfg.captcha_secret, "response": response,
                                       "remoteip": ip}).encode()
        try:
            with urllib.request.urlopen(CAPTCHA_URLS[self.cfg.captcha], data=data, timeout=5) as r:
                return bool(json.loads(r.read()).get("success"))
        except Exception:
            return False

    # ---- rate limits ----
    def _count(self, key: str, since: float) -> int:
        return self.S.one("SELECT COUNT(*) n FROM rate_events WHERE key=? AND ts>=?", (key, since))["n"]

    def _hit(self, *keys):
        t = _now()
        with self.S.lock:
            self.S.db.executemany("INSERT INTO rate_events (key, ts) VALUES (?,?)", [(k, t) for k in keys])
            self.S.db.execute("DELETE FROM rate_events WHERE ts < ?", (t - max(self.cfg.rl_window, 3600) * 2,))
            self.S.db.execute("DELETE FROM used_tokens WHERE ts < ?", (t - self.cfg.token_max_age * 2,))
            self.S.db.commit()

    # ---- main gate ----
    def reject(self, request: Request, form: str, reason: str, audit):
        audit(None, "spam_rejected", form, None, reason=reason, ip=self.ip_hash(request))
        raise HTTPException(400, GENERIC)

    def check(self, request: Request, form: str, fields: dict, audit) -> str:
        """Raise the generic 400 if anything fails. Returns the IP hash."""
        iph = self.ip_hash(request)
        if not self.cfg.enabled:
            return iph
        since = _now() - self.cfg.rl_window
        ip_key, g_key = f"{form}:ip:{iph}", f"{form}:global"
        over_ip = self._count(ip_key, since) >= self.cfg.rl_ip_max
        over_g = self._count(g_key, since) >= self.cfg.rl_global_max
        self._hit(ip_key, g_key)  # every attempt counts, including rejected ones
        if over_ip:
            self.reject(request, form, "rate_limit_ip", audit)
        if over_g:
            self.reject(request, form, "rate_limit_global", audit)
        if (fields.get("website") or "").strip():
            self.reject(request, form, "honeypot", audit)
        tok = fields.get("form_token") or ""
        try:
            data = self.ser.loads(tok, max_age=self.cfg.token_max_age)
        except SignatureExpired:
            self.reject(request, form, "token_expired", audit)
        except BadSignature:
            self.reject(request, form, "token_invalid", audit)
        if data.get("f") != form:
            self.reject(request, form, "token_wrong_form", audit)
        if _now() - float(data.get("t", 0)) < self.cfg.min_fill:
            self.reject(request, form, "too_fast", audit)
        if not self.pow_ok(tok, fields.get("pow_nonce") or "", self.cfg.pow_bits):
            self.reject(request, form, "pow_failed", audit)
        if self.cfg.captcha in CAPTCHA_URLS:
            if not self.verify_captcha(fields.get("captcha_response") or "", self.client_ip(request)):
                self.reject(request, form, "captcha_failed", audit)
        try:
            self.S.x("INSERT INTO used_tokens (nonce, ts) VALUES (?,?)", (data["n"], _now()))
        except Exception:
            self.reject(request, form, "token_reused", audit)
        return iph

    # ---- uploads ----
    def check_image(self, request: Request, form: str, data: bytes, audit) -> dict:
        """Validate an upload without fully trusting it. Returns {format, width, height, dhash, sha256}."""
        if not data or len(data) > self.cfg.max_upload:
            self.reject(request, form, "file_size", audit)
        head = data[:12]
        if head.startswith(b"\xff\xd8\xff"):
            fmt = "JPEG"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "PNG"
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            fmt = "WEBP"
        else:
            self.reject(request, form, "file_type", audit)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                im = Image.open(io.BytesIO(data))  # lazy: reads the header only; we check size before decoding
            w, h = im.size
        except Image.DecompressionBombError:
            self.reject(request, form, "decompression_bomb_or_too_large", audit)
        except Exception:
            self.reject(request, form, "file_unreadable", audit)
        if im.format != fmt:
            self.reject(request, form, "file_type_mismatch", audit)
        if w * h > self.cfg.max_pixels or max(w, h) > self.cfg.max_side:
            self.reject(request, form, "decompression_bomb_or_too_large", audit)
        if min(w, h) < self.cfg.min_side:
            self.reject(request, form, "image_too_small", audit)
        try:
            im.load()
        except Exception:
            self.reject(request, form, "file_corrupt", audit)
        return {"format": fmt, "width": w, "height": h, "dhash": dhash(im), "sha256": hashlib.sha256(data).hexdigest()}


def dhash(im: Image.Image) -> str:
    """64-bit difference hash (perceptual), hex."""
    g = ImageOps.exif_transpose(im).convert("L").resize((9, 8), Image.BILINEAR)
    px = [int(v) for v in __import__('numpy').asarray(g).reshape(-1)]
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
