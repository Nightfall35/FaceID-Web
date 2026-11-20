# Deploy guide: FastAPI + onnxruntime (~300 MB models) on the cheap

Notes for Sunny on hosting FaceID-Web (FastAPI, InsightFace / onnxruntime on CPU, SQLite gallery). Limits and prices were checked against official docs on 2026-11-20 (America/Los_Angeles) and will change; re-read the linked pages before paying or locking a platform.

This is a personal biometric app. Prefer a **private** deployment, a machine with **enough RAM to load ~326 MB of ONNX models plus Python**, and a **persistent volume** for SQLite. Ephemeral disks will wipe the gallery on every restart or sleep.

> **How this maps to this repo:** the repo's own `Dockerfile` already follows this guide (UID 1000,
> models baked at build time via `scripts/download_models.sh`, listens on `$PORT`, data on `/data`).
> The app reads `FACEID_DATA_DIR` (SQLite + photos; default `/data` in Docker), `FACEID_PASSWORD`,
> `FACEID_SECRET`, optional `FACEID_OPERATOR_NAME` (display name on the login loader, default `ALTAIR`;
> PowerShell: `$env:FACEID_OPERATOR_NAME="Altair"`), and the ASGI entry point is `app.main:app`. Only the two models FaceID uses
> (~190 MB: `det_10g.onnx`, `w600k_r50.onnx`) are baked, not the whole 326 MB pack.
> The sketch below is kept for reference; prefer the repo's Dockerfile.

## 1. Resource reality check

`buffalo_l` is listed at **326 MB** in the [InsightFace model zoo](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md). In process RSS you also pay for onnxruntime, OpenCV, NumPy, the FastAPI stack, and workspace for detection at (typically) 640 px. Plan for **at least 1 GB RAM, 2 GB if you can**.

Free tiers that cap a service at **512 MB** (Render Free, Railway Free) are likely to OOM once the pack is loaded. Treat them as "maybe a health-check demo," not a place to run the real app.

Bake the model pack into the image (or a read-only volume) so a cold start does not depend on GitHub / Google Drive. Put only mutable data (SQLite, optional encrypted photo store) on a persistent volume.

Also remember the pack license: InsightFace **code** is MIT; **pretrained `buffalo_l` weights are non-commercial research only** ([model zoo license note](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md)). A public hosted service is a worse fit than a private personal deployment.

## 2. Platform comparison (current official limits)

| Platform | What "free" actually means | CPU / RAM | Sleep / idle | Persistent storage for SQLite | Fit for this app |
| --- | --- | --- | --- | --- | --- |
| **Hugging Face Spaces (Docker)** | CPU Basic hardware is $0/hour, but **creating a Gradio or Docker Space requires a paid plan** (PRO for a personal account, currently **$9/month**; Team/Enterprise for orgs). Static Spaces stay free. Free accounts may still host up to 2 Gradio Spaces on ZeroGPU. | CPU Basic: **2 vCPU, 16 GB RAM, 50 GB ephemeral disk** | On CPU Basic, the Space sleeps after **48 hours** of inactivity; a visitor wakes it. Sleep time is not configurable on CPU Basic. | Default disk is **ephemeral** (lost on restart/stop). Attach a [Storage Bucket](https://huggingface.co/docs/hub/spaces-storage) as a read/write volume at runtime (not during Docker build). Private Hub storage: 100 GB on a free account, **1 TB included with PRO**, then ~$18/TB-month. | Best "free compute" RAM for CPU inference, but it is not a $0 path anymore, and SQLite needs a bucket (or an external DB). Use **Private** visibility. |
| **Render** | Free web service exists, plus 750 Free instance-hours per workspace per month. Officially for previews, not production. | Free plan: **0.1 CPU, 512 MB RAM**. Next paid web plans: 0.5 CPU / 512 MB at **$7/month**, 1 CPU / 2 GB at **$25/month**. | Free services spin down after **15 minutes** without HTTP/WebSocket traffic and take **about a minute** to wake. Render may restart Free services at any time. Spun-down time does not consume the 750 hours. | Filesystem is ephemeral; **Free services cannot attach persistent disks**. Paid services can, at **$0.25/GB-month**, encrypted at rest, with daily snapshots kept 7 days. Disks disable zero-downtime deploys (single instance). Free Postgres is 1 GB and **expires in 30 days**. | 512 MB is likely too small. A workable Render setup is roughly **$25/month** (2 GB) plus a small disk. |
| **Fly.io** | **No free tier** and no monthly free-usage allowance. New accounts get a **$1 trial credit** (no card). After a card is on file, **personal-org bills under $5 are waived** (a $5.00+ bill is charged in full). Other orgs do not get the waiver. | You choose size. Cheapest always-on in `iad`/`ewr`: `shared-cpu-1x` **256 MB $2.19**, **512 MB $3.69**, **1 GB $6.70**, `shared-cpu-2x` **2 GB $13.39** per 30 days, billed by the second while running. Stopped machines still pay **$0.15/GB-month** of rootfs. | Auto-stop/start is supported; you pay compute only while the machine runs, which can keep a lightly used app under the $5 waiver. First request after a stop still pays a cold start (models must reload). | [Fly Volumes](https://docs.fly.io/volumes/overview): default **1 GB**, max 500 GB, **$0.15/GB-month** provisioned, **encrypted at rest by default**, one volume per machine. Daily snapshots (default 5 days, first 10 GB of snapshot storage free). Rootfs is ephemeral. | Strong option if you size at **1–2 GB RAM** and mount SQLite on a volume. A 1 GB always-on machine ($6.70) plus a 1 GB volume ($0.15) already exceeds the $5 waiver unless you auto-stop. |
| **Railway** | Free plan: **$1 usage credit / month**, no card, 1 project, up to 3 services. New accounts also get a 30-day trial with a one-time **$5** grant. Hobby is **$5/month including $5 usage**, always-on. | Free: each service up to **1 vCPU and 0.5 GB RAM**. $1 of credit is only about 100 MB RAM around the clock at list rates, so a waking 0.5 GB service will burn the credit quickly. | Free services sleep after **5–10 minutes** without outbound traffic and wake on the next request. Trial/Hobby can stay always-on (Hobby). | Free plan includes **one 0.5 GB volume** (and a 10 GB bucket). Volume storage also draws from the $1 credit. | Free RAM is too tight. Hobby can work if you give the service ≥1 GB and stay within the included usage; confirm current Hobby resource caps on their pricing page before committing. |

Official pages: [Spaces overview](https://huggingface.co/docs/hub/spaces-overview), [Spaces GPUs / CPU Basic sleep](https://huggingface.co/docs/hub/spaces-gpus), [Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker), [HF PRO](https://huggingface.co/Pro), [HF storage limits](https://huggingface.co/docs/hub/storage-limits), [Render free](https://render.com/docs/free), [Render compute plans](https://render.com/docs/compute-plans), [Render disks](https://render.com/docs/disks), [Render pricing](https://render.com/pricing), [Fly pricing](https://docs.fly.io/about/pricing), [Fly $5 waiver](https://docs.fly.io/about/free-waiver), [Fly volumes](https://docs.fly.io/volumes/overview), [Railway free plan](https://docs.railway.com/pricing/free-plan).

**Practical pick for FaceID-Web**

- If Altair already wants a Hugging Face account and can pay PRO: **Docker Space on CPU Basic** is the most RAM-generous cheap host. Keep the Space **private**, bake models into the image, put SQLite on a Storage Bucket, and accept 48-hour sleep (or pay for always-on hardware).
- If you want a normal private URL and a real SQLite file: **Fly.io** with `shared-cpu-1x` or `shared-cpu-2x` at 1–2 GB RAM, a 1 GB volume, and auto-stop if you need to stay near the waiver.
- Render is straightforward but the first plan with enough RAM is **$25/month**.
- A small always-on VPS (2 GB RAM, 10–20 GB disk) is often the most predictable cheap home for SQLite and large models if you are willing to manage TLS and updates yourself.

## 3. Dockerfile sketch (works on all of the above)

Bake models at build time. Downloading `buffalo_l` on first request makes cold starts slow and can fail if outbound network or model hosts are flaky. Hugging Face Docker Spaces also restrict outbound ports (80, 443, and 8080).

Hugging Face images should run as UID 1000. Listen on `PORT` (Render/Railway/Fly) and default to `7860` for Spaces (`app_port` in the Space README).

```dockerfile
FROM python:3.11-slim-bookworm

# HF Spaces expects UID 1000; this is harmless on Fly/Render/Railway.
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    INSIGHTFACE_HOME=/home/user/.insightface \
    FACEID_DB=/data/faceid.sqlite

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

USER user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the pack (326 MB) so runtime does not download it.
# Place the unzipped buffalo_l files in ./models/buffalo_l in the build context.
COPY --chown=user models/buffalo_l/ ${INSIGHTFACE_HOME}/models/buffalo_l/
COPY --chown=user . .

# /data is the persistent mount (create a writable dir for local dev).
USER root
RUN mkdir -p /data && chown user:user /data
USER user

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
```

Point the app at `FACEID_DB` (or `sqlite:////data/faceid.sqlite`). Enable WAL mode (`PRAGMA journal_mode=WAL`) so a crash mid-write is less likely to corrupt the gallery. Do not store the SQLite file in the image or in `/tmp`.

**Bake vs download at startup**

- **Bake (recommended):** larger image, slower builds, reliable/offline boot, no secrets needed to fetch models.
- **Download at startup:** smaller git repo, but every sleep/wake re-fetches unless you cache on a volume; first request can time out; model hosts may be blocked. If you must download, do it once onto the persistent volume and skip when the files already exist.

Keep enrollment photos off the image. If you store any raw images, put them on the same encrypted volume, not in the repo.

## 4. Short platform steps

### Hugging Face Spaces (Docker)

1. Subscribe to [PRO](https://huggingface.co/Pro) (or use a Team/Enterprise org).
2. Create a Space, SDK **Docker**, visibility **Private**, hardware **CPU Basic**.
3. Set `sdk: docker` and `app_port: 7860` in the README YAML ([Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker)).
4. Push the Dockerfile above. Confirm the container process listens on 7860.
5. Attach a **private Storage Bucket** as a read/write volume (for example `/data`) in Space settings. Buckets are available at runtime only, not during `docker build`.
6. Put secrets (auth credentials, signing keys) in Space **Secrets**, never in the Dockerfile or git.
7. Expect a cold start after 48 hours idle and after every rebuild (ephemeral disk is wiped; the bucket is not).

### Render

1. Do not use the Free web service for the real app (512 MB, 15-minute sleep, no disk, gallery would vanish).
2. Create a paid **Web Service** with at least the **1 CPU / 2 GB** plan, Docker runtime, start command as in the Dockerfile.
3. Attach a persistent disk, mount path `/data`, smallest size that holds SQLite and any photos ([disks](https://render.com/docs/disks)). You can grow a disk later, not shrink it.
4. Set `FACEID_DB=/data/faceid.sqlite` and auth secrets in the dashboard.
5. Accept a short outage on deploy: a disk-backed service stops the old instance before starting the new one.

### Fly.io

1. `fly launch` with the Dockerfile; do not use 256–512 MB.
2. Example (verify current syntax in Fly docs): a `shared-cpu-1x` or `shared-cpu-2x` machine with **1024–2048 MB** RAM, auto-stop if you want idle cost close to zero, and a volume:

   ```text
   fly volumes create faceid_data --size 1 --region iad
   ```

   Mount it at `/data` in `fly.toml`. Volumes are encrypted at rest by default; snapshots are not a substitute for your own backup copy of the SQLite file.
3. A personal-org bill under $5 is waived; an always-on 1 GB machine already exceeds that before traffic. Use auto-stop or expect to pay the full month.
4. One volume attaches to one machine. Fine for a single-instance SQLite app; do not scale to two machines sharing one DB file.

### Railway

1. Upgrade past the Free 0.5 GB cap (Hobby unless you confirm another plan).
2. Deploy the Dockerfile, attach the project's volume at `/data`, and read `RAILWAY_VOLUME_MOUNT_PATH` if you want the path to be configurable ([volumes](https://docs.railway.com/volumes)).
3. Watch the usage meter; on credit-limited plans the app stops when the credit runs out.

## 5. Privacy: storing face data responsibly

This section is practical hygiene plus pointers to law. It is **not legal advice**. Obligations depend on who operates the app, where they and the enrolled people live, and whether the app is purely personal. If FaceID-Web is only ever used by Altair on a private machine, the risk is lower than a public website, but consent, deletion, and access control are still the right defaults.

### Product rules to implement

- **Consent records.** Before enrollment, record who was enrolled, who consented (the subject or a lawful representative), when, the purpose (e.g. "label this person's face in photos I upload"), and the retention period. Keep that record even if you later delete the raw photo. Do not enroll a photo of someone who did not agree.
- **Store embeddings, not photos, when you can.** A 512-d vector is enough to recognize; it is still biometric data, but it avoids keeping a growing library of identifiable images. If you keep raw or aligned crops (for re-enrollment or audit), store them separately, encrypted, with a deletion path, and do not bake them into the image or a public repo.
- **Encryption.** Use TLS in transit. Prefer a volume with encryption at rest (Fly default; Render disks are encrypted at rest; confirm the setting on whatever host you choose). Consider encrypting embedding blobs or the whole SQLite file at the application layer (for example SQLCipher or a sealed store) so a copied volume file is not immediately usable. Protect backups the same way.
- **Access control.** The API must not be an open endpoint on the public internet. Add authentication (at least a strong password or better, SSO / magic link) on enrollment, search, and gallery export. Private Space / private Fly app is not a substitute for app-level auth if the URL can leak. Log access to enrollment and deletion events.
- **Deletion on request.** Implement a hard delete: all embeddings, photos, and derived indexes for that person, then vacuum or wipe free space where practical. Confirm deletion to the requester. Also support "delete everything" for the gallery.
- **Retention.** Define a written retention period and actually delete when the purpose is over (or on the stated schedule). Do not keep probes (uploaded search photos) longer than needed to show results.
- **Least data in logs.** Do not log embeddings, photos, or filenames that identify a person in plaintext application logs.

### Legal notes (read the primary text)

**Illinois BIPA** ([740 ILCS 14](https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=3004&ChapterID=57)) is the best-known U.S. biometric privacy statute and has a private right of action. A "scan of … face geometry" is a biometric identifier; information derived from it and used to identify a person is "biometric information." Photographs themselves are excluded, but embeddings computed from photos to identify someone are the kind of data the Act targets. If BIPA applies, a private entity must, before collection:

1. inform the person in writing that a biometric identifier/information is being collected or stored,
2. inform them in writing of the **specific purpose and length of term**, and
3. obtain a **written release** (informed written consent, including an electronic signature).

The entity must also publish a retention and destruction policy (destroy when the initial purpose is satisfied or within **three years** of last interaction, whichever is first), must not sell or otherwise profit from the data, may disclose only with consent or a narrow legal exception, and must protect the data at least as strongly as other confidential information. Negligent violations can be $1,000 per violation, reckless/intentional $5,000, plus fees.

**GDPR / UK GDPR.** Biometric data processed **for the purpose of uniquely identifying** a person is special-category data. Official text: [Regulation (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj). See Article 4(14) (definition of biometric data) and Article 9 (prohibition on processing special-category data unless an exception applies, commonly **explicit consent** in Article 9(2)(a)). Recital 51 is the usual reminder that a photograph is not automatically biometric data until it is processed by specific technical means that allow unique identification — which a face-embedding pipeline is. The UK ICO's guidance is a clear practitioner summary: [How do we process biometric data lawfully?](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/biometric-data-guidance-biometric-recognition/how-do-we-process-biometric-data-lawfully). You also need a lawful basis under Article 6, a way to honor access and **erasure** (Article 17), a retention limit, and usually a note that people can withdraw consent. Hosting face data on a U.S. platform can raise international-transfer issues if GDPR applies.

Other U.S. states have biometric or consumer-privacy rules (for example Texas, Washington, and California CPRA "sensitive personal information"). If enrolled people live in those places, check those statutes too.

If this stays a private, consented, non-commercial tool for Altair, still: get consent in writing, lock the app down, store vectors rather than a photo archive when possible, encrypt, and delete on request. If it is ever offered to others or used in a workplace, get actual legal advice before collecting more faces.

## Sources

- InsightFace pack size and license: <https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md>, <https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md>
- Hugging Face Spaces overview and hardware: <https://huggingface.co/docs/hub/spaces-overview>
- Spaces sleep / CPU Basic: <https://huggingface.co/docs/hub/spaces-gpus>
- Docker Spaces, port, UID 1000, persistence: <https://huggingface.co/docs/hub/spaces-sdks-docker>
- Spaces storage / buckets: <https://huggingface.co/docs/hub/spaces-storage>
- Hub storage limits and private storage allowances: <https://huggingface.co/docs/hub/storage-limits>
- HF PRO pricing: <https://huggingface.co/Pro>
- Render free tier: <https://render.com/docs/free>
- Render compute plans: <https://render.com/docs/compute-plans>
- Render persistent disks: <https://render.com/docs/disks>
- Render pricing: <https://render.com/pricing>
- Fly.io pricing: <https://docs.fly.io/about/pricing>
- Fly.io $5 waiver: <https://docs.fly.io/about/free-waiver>
- Fly Volumes: <https://docs.fly.io/volumes/overview>
- Railway Free plan: <https://docs.railway.com/pricing/free-plan>
- Railway volumes: <https://docs.railway.com/volumes>
- Illinois BIPA: <https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=3004&ChapterID=57>
- GDPR official text: <https://eur-lex.europa.eu/eli/reg/2016/679/oj>
- ICO biometric recognition guidance: <https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/biometric-data-guidance-biometric-recognition/how-do-we-process-biometric-data-lawfully>
