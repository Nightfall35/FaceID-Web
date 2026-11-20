# Missing-persons pipeline (consent-based)

A module on top of FaceID-Web that matches photos of **verified missing-person cases** against **sighting photos
people submit voluntarily**, with a human reviewer between every match and the family. Page: `http://localhost:<port>/mp`.

## What it is not
* It does **not** scrape, crawl or bulk-import photos from the web, social media or cameras. There is no import API.
* It does **not** track anyone or release a location automatically. A location reaches a family only when a human
  reviewer approves that specific match.

## Data flow
```
family ──(case + photos)──► cases [pending] ──admin verifies (police report no.)──► [verified]
                                                                                       │  auto-match vs recent sightings
public ──(sighting photo, consent, optional location)──► sightings ──auto-match vs verified cases──┐
                                                                                       ▼            ▼
                                                                      matches [pending] ──► reviewer queue
                                                                                       │ approve      │ reject
                                                                                       ▼              ▼
                                                           family sees photo + location     never shown
```

## Roles
| Role | Can |
|---|---|
| `admin` | create users, verify / reject / close cases (must record a verification reference), run retention purge, read the audit log, everything a reviewer can |
| `reviewer` | see the queue (case photos side-by-side with sighting, score, location), approve / reject matches, confirm / reject self-reports |
| `family` (case owner) | open cases, see **only their own** cases and **only reviewer-approved** sightings, close their case |
| public (no login) | submit a sighting (consent checkbox required), submit a self-report |

## Safety gates (all covered by `tests/test_missing_persons.py`)
1. **Verified cases only.** Matching queries only `cases.status='verified'`. Pending, rejected and closed cases are never matched; rejecting a case also deletes its embeddings.
2. **Verification reference.** `POST /api/mp/admin/cases/{id}/verify` requires a reference (e.g. a police report number). It is stored with the admin's id and time.
3. **No location before approval.** Sighting submitters get the same "received" reply whether or not anything matched. Families get sighting data only from `/api/mp/my/cases/{id}/updates`, which returns only `approved` matches. Sighting photo files are only served to a family for their own approved matches. In-app notifications and the webhook/email payloads never include a location or photo.
4. **Owners never see raw or unreviewed data.** The queue, sightings, self-reports, audit log and other families' cases all return 401/403/404 for family accounts.
5. **"I'm safe, don't locate me".** `POST /api/mp/self-report` takes the public case code, plus an optional photo and message. The reply is identical for valid and invalid codes, so case codes can't be probed. While it is pending, the case is **on hold**: no match can be approved. A reviewer confirms it by comparing the photo with the case photos and a similarity score. The case is then closed, **all** matches are withdrawn (including ones approved earlier), the family sees only "closed at the person's request", and embeddings and the self-report photo are deleted.
6. **Embeddings are deleted on close**, for the case photos and for sightings that were only linked to that case.
7. **Retention.** Sightings older than `FACEID_MP_RETENTION_DAYS` (default 30) with no pending or approved match are deleted, along with their files. This runs at startup, after every sighting, and from the admin "Run retention purge" button.
8. **Audit log.** Logins, failed logins, user creation, case create/verify/reject/close, sightings, matches, reviews, self-reports, owner views and purges are all recorded in `audit_log`.

## Group photos
* Every face in a sighting photo is detected and embedded, not just the largest. Each face is a row in `sighting_faces` with its index, a bounding box (as fractions of the image), its detector score and its embedding.
* Faces smaller than `FACEID_MP_MIN_FACE_PX` (40 px on the short side) or below `FACEID_MP_MIN_DET_SCORE` (0.6) are skipped and never stored. Each photo keeps at most `FACEID_MP_MAX_FACES` (20) faces, largest first.
* Each case gets at most one match per photo, using its best-scoring face. The match stores `face_id` and `face_index`.
* **Bystander biometrics.** As soon as a photo has a live (pending or approved) match, the embeddings of every other face in it are deleted. This is audited as `bystander_embeddings_deleted`. A face whose match is rejected loses its embedding too. A photo with no match keeps its face embeddings until retention expires, so a case verified later can still be matched against it.
* Retention and case closure work per face. Purging a sighting deletes all its face rows. Closing a case deletes the embeddings of the faces matched to it.
* **Reviewers** see the whole group photo with the matched face highlighted, the other faces outlined, and a close-up crop (`/api/mp/files/face/{match_id}`).
* **Families** see only approved matches. They get the photo with every other face pixelated and blurred on the server, plus a crop of the matched face. They never receive the original file.
* Stored photos are re-encoded as JPEG with EXIF orientation applied and all metadata stripped, including GPS tags embedded in the camera file.

## Spam protection (public sighting and self-report forms)
No third-party service is needed. The code is in `app/antispam.py`, with the client side in `static/mp-guard.js`.

| Layer | How it works | Settings |
|---|---|---|
| Form token | `GET /api/mp/form-token?form=sighting\|self` returns a signed, single-use token for one form. Nonces are stored in SQLite, so a token can't be replayed. | `FACEID_MP_TOKEN_MAX_AGE` (7200 s) |
| Minimum fill time | The token's signed timestamp must be at least N seconds old when the form is submitted. The page waits automatically if someone submits faster. | `FACEID_MP_MIN_FILL_SECONDS` (3) |
| Proof of work | The browser finds a nonce where `sha256(token + ":" + nonce)` starts with N zero bits. It starts working while the form is being filled in. The server checks it with a single hash. | `FACEID_MP_POW_BITS` (14; 0 turns it off) |
| Honeypot | A hidden `website` field that people never see. | – |
| Rate limits | A sliding window in SQLite, per IP and global, applied to each form separately. Rejected attempts count too. | `FACEID_MP_RL_WINDOW_SECONDS` (600), `FACEID_MP_RL_IP_MAX` (10), `FACEID_MP_RL_GLOBAL_MAX` (300), `FACEID_MP_TRUST_PROXY` (0; set to 1 only behind your own reverse proxy so `X-Forwarded-For` is used) |
| Upload checks | Size limit; type checked from magic bytes (JPEG, PNG or WebP only) and matched against the decoded format; width, height and total pixels checked from the header *before* decoding, which blocks decompression bombs; minimum side; and the file must fully decode. | `FACEID_MP_MAX_UPLOAD_MB` (10), `FACEID_MP_MAX_SIDE` (8000), `FACEID_MP_MAX_PIXELS` (40000000), `FACEID_MP_MIN_SIDE` (32) |
| Duplicate submissions | The same file (SHA-256) or a near-identical picture (64-bit dHash within N bits) from the same IP inside the window is rejected. If different people send near-identical photos, they're kept and grouped as duplicates instead (see Automation). | `FACEID_MP_DUP_WINDOW_HOURS` (24), `FACEID_MP_DUP_HAMMING` (4) |
| Captcha (optional) | hCaptcha or Cloudflare Turnstile, checked on the server. Off by default. | `FACEID_MP_CAPTCHA` (`hcaptcha` / `turnstile`), `FACEID_MP_CAPTCHA_SITEKEY`, `FACEID_MP_CAPTCHA_SECRET` |

Every rejection returns the **same** HTTP 400 and message, whichever check failed. Each one is written to the audit log as `spam_rejected` with the internal reason. IP addresses are never stored raw, only an HMAC keyed with `FACEID_SECRET`. `FACEID_MP_ANTISPAM=0` turns off all form checks; it's meant for tests only. The upload checks always run.

## Automation
* Sighting intake extracts an embedding for every usable face with the existing InsightFace (SCRFD + ArcFace) pipeline (see Group photos).
* A new sighting is matched against every verified open case. A newly verified case is matched against sightings from the last `FACEID_MP_MATCH_WINDOW_DAYS`.
* Score = max cosine similarity over the case photos. A match is a candidate when score ≥ `FACEID_MP_THRESHOLD` (default 0.45). The queue is ranked by score.
* Dedup: a sighting is stored as a duplicate when it has the same image hash as a sighting from the last `FACEID_MP_DEDUP_HOURS` (24), or when every one of its faces has similarity ≥ `FACEID_MP_DEDUP_SIM` (0.97) to a face in such a sighting. It isn't re-queued and shows as "+N near-duplicates".
* Notifications: there's an in-app list (`GET /api/mp/notifications`). There's also an optional webhook, `FACEID_MP_WEBHOOK_URL` (JSON POST), and an email stub, `FACEID_MP_NOTIFY_EMAIL` (log only). Both are disabled by default.

## Storage
A separate SQLite file, `data/missing_persons.sqlite3`, holds the tables `users, cases, case_photos, sightings, sighting_faces, matches, reviews, self_reports, notifications, audit_log, rate_events, used_tokens`. A database created by the first version is upgraded automatically: the new columns are added and the new tables created. Images are in `data/mp_files/`. The existing `faceid.sqlite3` is never touched.

## Run it (Windows PowerShell)
```powershell
$env:FACEID_MP_ADMIN_PASSWORD = "choose-a-long-admin-password"   # creates user 'admin' on first start
$env:FACEID_MP_RETENTION_DAYS = "30"      # optional
$env:FACEID_MP_THRESHOLD = "0.45"         # optional
# group photos (optional)
$env:FACEID_MP_MAX_FACES = "20"; $env:FACEID_MP_MIN_FACE_PX = "40"; $env:FACEID_MP_MIN_DET_SCORE = "0.6"
# spam protection (optional; these are the defaults)
$env:FACEID_MP_POW_BITS = "14"; $env:FACEID_MP_MIN_FILL_SECONDS = "3"
$env:FACEID_MP_RL_IP_MAX = "10"; $env:FACEID_MP_RL_GLOBAL_MAX = "300"; $env:FACEID_MP_RL_WINDOW_SECONDS = "600"
$env:FACEID_MP_MAX_UPLOAD_MB = "10"; $env:FACEID_MP_MAX_PIXELS = "40000000"
# $env:FACEID_MP_CAPTCHA = "turnstile"; $env:FACEID_MP_CAPTCHA_SITEKEY = "..."; $env:FACEID_MP_CAPTCHA_SECRET = "..."
# $env:FACEID_MP_TRUST_PROXY = "1"   # only behind your own reverse proxy
$env:PORT = "8123"
.\run.ps1
# open http://localhost:8123/mp  -> Sign in as admin -> Verification -> add reviewer accounts
```
Families can create their own account on the sign-in screen. Set `FACEID_MP_OPEN_REGISTRATION=0` to make admins create them instead.

**Demo with synthetic data** (silhouettes, mock embeddings, temporary data dir, no models needed):
```powershell
.\.venv\Scripts\python.exe scripts\mp_demo.py --port 8123
# admin / demo-admin-pass · reviewer1 / demo-review-pass · family1 / demo-family-pass
```

**Tests**: `.\.venv\Scripts\python.exe -m pytest -q`. The tests are `test_missing_persons.py` (safety gates), `test_mp_group_faces.py` and `test_mp_antispam.py`. They use synthetic noise images and a mock face detector, with no face datasets.

## Before real use
* Put it behind HTTPS and keep the admin password long. Sessions are signed cookies (`FACEID_SECRET`).
* Agree on the verification process (who checks police reports) and on the reviewer training and policy.
* The public forms have rate limits, proof of work and a honeypot. For a high-traffic public deployment, also turn on Turnstile or hCaptcha.
* Check local law on biometric data (for example GDPR Art. 9, BIPA or CCPA) and publish a privacy notice on the sighting page.
