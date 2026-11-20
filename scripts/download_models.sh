#!/usr/bin/env bash
# Downloads InsightFace buffalo_l and keeps the two models FaceID uses:
#   det_10g.onnx   - SCRFD-10G face detector (5 landmarks)
#   w600k_r50.onnx - ArcFace ResNet-50 trained on WebFace600K
set -euo pipefail
DIR="${FACEID_MODEL_DIR:-$(cd "$(dirname "$0")/.." && pwd)/models}"
mkdir -p "$DIR"
if [[ -f "$DIR/det_10g.onnx" && -f "$DIR/w600k_r50.onnx" ]]; then echo "Models present in $DIR"; exit 0; fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "Downloading buffalo_l (~280 MB)…"
curl -fL --retry 3 -o "$TMP/buffalo_l.zip" https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip
python3 - "$TMP/buffalo_l.zip" "$DIR" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for n in z.namelist():
    if n.split('/')[-1] in ("det_10g.onnx", "w600k_r50.onnx"):
        open(f"{sys.argv[2]}/{n.split('/')[-1]}", "wb").write(z.read(n))
PY
ls -la "$DIR"
