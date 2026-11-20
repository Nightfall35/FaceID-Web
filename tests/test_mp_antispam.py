"""Spam protection on the public forms: every check rejects with the same response and is audited."""
import io

import pytest
from PIL import Image

from mp_common import (group_photo, identity, make_env, noise_png, solve_pow, spam_fields, submit_sighting)

GOOD_FACE = lambda seed: [((0.2, 0.2, 0.6, 0.7), identity(seed), 80, 0.9)]


def reasons(env):
    return [a["details"].get("reason") for a in env["admin"].get("/api/mp/admin/audit?limit=500").json()["audit"]
            if a["action"] == "spam_rejected"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch, FACEID_MP_POW_BITS=10, FACEID_MP_RL_IP_MAX=50)


def test_valid_submission_passes(env):
    r = submit_sighting(env["public"], group_photo(GOOD_FACE(1)))
    assert r.status_code == 200, r.text
    t = env["public"].get("/api/mp/form-token?form=sighting").json()
    assert t["pow_bits"] == 10 and t["captcha"] is None


def test_each_check_rejects_identically(env):
    pub = env["public"]
    bodies = []

    def post(fields, data=None):
        r = pub.post("/api/mp/sightings", files={"file": ("s.png", data or group_photo(GOOD_FACE(2)), "image/png")},
                     data={"consent": "true", **fields})
        bodies.append((r.status_code, r.text))
        return r

    post({})                                                       # no token
    post({**spam_fields(pub), "website": "http://spam.example"})   # honeypot
    f = spam_fields(pub); post({**f, "pow_nonce": "nope"})         # bad PoW
    post({**spam_fields(pub, form="self")})                        # token for another form
    f = spam_fields(pub)
    assert post(f).status_code == 200                              # valid ...
    bodies.pop()
    post(f)                                                        # ... but the token is single use
    post({**f, "form_token": f["form_token"][:-3] + "abc"})        # tampered token
    assert len(set(bodies)) == 1 and bodies[0][0] == 400
    got = reasons(env)
    for r in ("token_invalid", "honeypot", "pow_failed", "token_wrong_form", "token_reused"):
        assert r in got, r
    # the response never names the check
    for word in ("honeypot", "pow", "token", "rate"):
        assert word not in bodies[0][1].lower()


def test_min_fill_time(env, monkeypatch):
    import app.antispam as asp
    monkeypatch.setattr(env["mp"].GUARD.cfg, "min_fill", 5)
    clock = [1_000_000.0]
    monkeypatch.setattr(asp, "_now", lambda: clock[0])
    pub = env["public"]
    f = spam_fields(pub)
    r = submit_sighting(pub, group_photo(GOOD_FACE(3)), extra=f)
    assert r.status_code == 400 and "too_fast" in reasons(env)
    f = spam_fields(pub)
    clock[0] += 6
    assert submit_sighting(pub, group_photo(GOOD_FACE(4)), extra=f).status_code == 200


def test_rate_limits(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch, FACEID_MP_RL_IP_MAX=3, FACEID_MP_RL_GLOBAL_MAX=100)
    codes = [submit_sighting(env["public"], group_photo(GOOD_FACE(10 + i))).status_code for i in range(5)]
    assert codes == [200, 200, 200, 400, 400]
    assert "rate_limit_ip" in reasons(env)
    # different IP (via trusted proxy header) is not blocked by the per-IP limit ...
    g = env["mp"].GUARD
    monkeypatch.setattr(g.cfg, "trust_proxy", True)
    other = env["client"](); other.headers["X-Forwarded-For"] = "203.0.113.9"
    assert submit_sighting(other, group_photo(GOOD_FACE(20))).status_code == 200
    # ... but the global limit applies to everyone
    monkeypatch.setattr(g.cfg, "rl_global_max", 1)
    third = env["client"](); third.headers["X-Forwarded-For"] = "198.51.100.7"
    assert submit_sighting(third, group_photo(GOOD_FACE(21))).status_code == 400
    assert "rate_limit_global" in reasons(env)
    # raw IPs are never stored
    import json
    blob = json.dumps(env["admin"].get("/api/mp/admin/audit?limit=500").json())
    assert "203.0.113.9" not in blob and "198.51.100.7" not in blob


def test_upload_checks(env):
    pub = env["public"]
    def send(data, name="x.png", ctype="image/png"):
        return pub.post("/api/mp/sightings", files={"file": (name, data, ctype)},
                        data={"consent": "true", **spam_fields(pub)})
    assert send(b"GIF89a" + b"\0" * 100, "x.gif", "image/gif").status_code == 400     # type not allowed
    assert send(b"<?php echo 1; ?>" * 10).status_code == 400                            # not an image
    buf = io.BytesIO(); Image.new("1", (12000, 12000)).save(buf, "PNG")                 # 144 MP bomb, tiny file
    assert len(buf.getvalue()) < 2_000_000 and send(buf.getvalue()).status_code == 400
    buf = io.BytesIO(); Image.new("RGB", (16, 16)).save(buf, "PNG")                     # too small
    assert send(buf.getvalue()).status_code == 400
    jpg = io.BytesIO(); Image.new("RGB", (100, 100)).save(jpg, "JPEG")
    assert send(b"\x89PNG\r\n\x1a\n" + jpg.getvalue()[8:]).status_code == 400          # magic/format mismatch
    assert send(b"\0" * (11 * 1024 * 1024)).status_code == 400                         # too big
    got = reasons(env)
    for r in ("file_type", "decompression_bomb_or_too_large", "image_too_small", "file_size"):
        assert r in got, r


def test_duplicate_submission_same_ip(env):
    pub = env["public"]
    data = group_photo(GOOD_FACE(30))
    assert submit_sighting(pub, data).status_code == 200
    assert submit_sighting(pub, data).status_code == 400                      # exact same file
    im = Image.open(io.BytesIO(data)); buf = io.BytesIO(); im.save(buf, "JPEG", quality=70)
    import hashlib
    from mp_common import REGISTRY
    REGISTRY[hashlib.sha256(buf.getvalue()).hexdigest()] = REGISTRY[hashlib.sha256(data).hexdigest()]
    r = pub.post("/api/mp/sightings", files={"file": ("y.jpg", buf.getvalue(), "image/jpeg")},
                 data={"consent": "true", **spam_fields(pub)})
    assert r.status_code == 400                                                # re-encoded: perceptual hash
    assert reasons(env).count("duplicate_submission") == 2


def test_self_report_is_protected(env):
    pub = env["public"]
    assert pub.post("/api/mp/self-report", data={"case_ref": "MP-ABCDEF"}).status_code == 400
    assert pub.post("/api/mp/self-report", data={"case_ref": "MP-ABCDEF", **spam_fields(pub, "self"),
                                                 "website": "x"}).status_code == 400
    assert pub.post("/api/mp/self-report", data={"case_ref": "MP-ABCDEF", **spam_fields(pub, "self")}).status_code == 200


def test_captcha_hook(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch, FACEID_MP_CAPTCHA="turnstile", FACEID_MP_CAPTCHA_SITEKEY="site-key",
                   FACEID_MP_CAPTCHA_SECRET="secret")
    g = env["mp"].GUARD
    t = env["public"].get("/api/mp/form-token").json()
    assert t["captcha"] == {"provider": "turnstile", "sitekey": "site-key", "field": "cf-turnstile-response"}
    seen = []
    monkeypatch.setattr(g, "verify_captcha", lambda resp, ip: seen.append(resp) or resp == "good")
    assert submit_sighting(env["public"], group_photo(GOOD_FACE(40)), extra={"captcha_response": "bad"}).status_code == 400
    assert submit_sighting(env["public"], group_photo(GOOD_FACE(41)), extra={"captcha_response": "good"}).status_code == 200
    assert seen == ["bad", "good"] and "captcha_failed" in reasons(env)


def test_captcha_off_by_default(env):
    assert env["mp"].GUARD.cfg.captcha == ""


def test_pow_verifier():
    from app.antispam import SpamGuard
    tok = "abc"
    n = solve_pow(tok, 12)
    assert SpamGuard.pow_ok(tok, n, 12) and not SpamGuard.pow_ok(tok, str(int(n) + 1), 20)
