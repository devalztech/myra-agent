#!/usr/bin/env bash
# Installs Python dependencies + cloudflared. Safe to re-run on every boot.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKEND_DIR="$(pwd)"

PY="${PYTHON_BIN:-python3}"
PIP_FLAGS="--no-cache-dir --disable-pip-version-check"
STAMP=".install-stamp"
REQ_HASH="$("$PY" - <<'EOF'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())
EOF
)"

echo "==> Myra backend install"
echo "    python: $("$PY" --version 2>&1)"

if [[ "${MYRA_SKIP_INSTALL:-0}" == "1" ]]; then
  echo "==> MYRA_SKIP_INSTALL=1, skipping dependency install"
elif [[ -f "$STAMP" && "$(cat "$STAMP")" == "$REQ_HASH" ]]; then
  echo "==> Dependencies already up to date"
else
  echo "==> Installing Python requirements (this can take a few minutes on first boot)"
  "$PY" -m pip install --upgrade pip $PIP_FLAGS
  # llama-cpp-python ships prebuilt CPU wheels on this index; falls back to source.
  "$PY" -m pip install $PIP_FLAGS \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
    -r requirements.txt
  echo "$REQ_HASH" > "$STAMP"
fi

# Cloudflare tunnel: cloudflared is downloaded and started by app/main.py
# itself now (see _start_cloudflare_tunnel there), not here. That keeps
# tunnel setup working the same way regardless of which entrypoint actually
# runs — this script, `uvicorn app.main:app` directly, or a panel that
# executes `python app/main.py` as a bare script and skips this file
# entirely.

# ---- model ---------------------------------------------------------------
if [[ "${MYRA_SKIP_MODEL:-0}" == "1" || "${MYRA_LLM_BACKEND:-llama_cpp}" == "mock" ]]; then
  echo "==> Skipping model download"
else
  "$PY" scripts/download_model.py || echo "!! Model download failed — it will retry on first chat"
fi

echo "==> Install complete"
