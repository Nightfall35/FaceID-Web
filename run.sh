#!/usr/bin/env bash
# One-command local run:  ./run.sh            (http://localhost:8000)
#   FACEID_PASSWORD=secret PORT=9000 ./run.sh
#   FACEID_OPERATOR_NAME=Altair ./run.sh   (name shown on the login loader; default ALTAIR)
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [[ ! -d .venv ]]; then "$PY" -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip >/dev/null
pip install -q -r requirements.txt
./scripts/download_models.sh
if [[ -z "${FACEID_PASSWORD+x}" ]]; then
  export FACEID_PASSWORD="faceid"
  echo "⚠  FACEID_PASSWORD not set; using default password 'faceid'. Set FACEID_PASSWORD to change it (empty string disables login)."
fi
export FACEID_SECRET="${FACEID_SECRET:-$( [[ -f data/.secret ]] || { mkdir -p data; python -c 'import secrets;print(secrets.token_hex(32))' > data/.secret; }; cat data/.secret)}"
PORT="${PORT:-8000}"
echo "FaceID running on http://localhost:${PORT}"
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "$PORT"
