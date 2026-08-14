#!/usr/bin/env bash
# Installs Python dependencies + cloudflared. Safe to re-run on every boot.
#
# Myra is remote-only: no local llama model, no model download. Just the
# backend requirements + the cloudflare tunnel (started by app/main.py).
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
  "$PY" -m pip install $PIP_FLAGS -r requirements.txt
  # Browser engine for screenshots / page automation. Best-effort: if it
  # fails (missing system deps), Myra still works — just without screenshots.
  if "$PY" -m playwright install chromium --with-deps 2>>"$BACKEND_DIR/logs/install.err"; then
    echo "==> Playwright chromium installed"
  else
    echo "!! Playwright chromium install failed (screenshots disabled until fixed)"
    "$PY" -m playwright install chromium 2>>"$BACKEND_DIR/logs/install.err" || true
  fi
  echo "$REQ_HASH" > "$STAMP"
fi

echo "==> Install complete"
