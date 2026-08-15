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
  echo "$REQ_HASH" > "$STAMP"
fi

# Chromium (Playwright) is installed in the BACKGROUND, off the boot path.
# On Pterodactyl there's no MYRA_SKIP_INSTALL guard like the Docker image
# gets, so this used to re-run on every single boot as part of the
# synchronous install step above — a multi-minute chromium download that
# blocked uvicorn from ever binding the port. Cloudflare (or the panel's own
# port check) hitting the tunnel during that window is exactly what produced
# the "Bad gateway" on first boot: nothing was listening yet.
#
# It's also best-effort and self-healing now (see
# app/services/browser_setup.py's ensure_chromium(), called by the browser
# and screenshot_file tools on first real use) — so there's no need for boot
# to wait on it at all. `--with-deps` is deliberately NOT used here: it
# needs apt + root, which this sandbox doesn't have, and just fails loudly
# for no benefit; plain `chromium` (headless_shell) runs fine without it.
mkdir -p "$BACKEND_DIR/logs"
CHROMIUM_STAMP="$BACKEND_DIR/.chromium-install-stamp"
if [[ "${MYRA_SKIP_INSTALL:-0}" != "1" && ! -f "$CHROMIUM_STAMP" ]]; then
  (
    if "$PY" -m playwright install chromium >>"$BACKEND_DIR/logs/install.log" 2>>"$BACKEND_DIR/logs/install.err"; then
      touch "$CHROMIUM_STAMP"
      echo "==> Playwright chromium installed (background)" >>"$BACKEND_DIR/logs/install.log"
    else
      echo "!! Playwright chromium install failed (background) — will retry on first tool use" >>"$BACKEND_DIR/logs/install.err"
    fi
  ) &
  disown
  echo "==> Chromium install started in background (not blocking boot)"
fi

# Pre-fetch cloudflared too so app/main.py's tunnel thread finds it already
# on disk the moment uvicorn is up, instead of downloading it (~40MB) AFTER
# the API is live — that gap is extra time the panel/Cloudflare could see
# "nothing answering yet" on a brand new deploy. Cheap and small, so this one
# runs synchronously (seconds, not minutes) rather than backgrounded.
CF_BIN_DIR="$BACKEND_DIR/.bin"
CF_BIN="$CF_BIN_DIR/cloudflared"
if [[ "${MYRA_SKIP_TUNNEL:-0}" != "1" && ! -x "$CF_BIN" ]]; then
  mkdir -p "$CF_BIN_DIR"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) arch="amd64" ;;
  esac
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
  echo "==> Pre-fetching cloudflared (${arch})"
  if curl -fsSL "$url" -o "$CF_BIN.tmp" 2>>"$BACKEND_DIR/logs/install.err"; then
    chmod +x "$CF_BIN.tmp"
    mv "$CF_BIN.tmp" "$CF_BIN"
    echo "==> cloudflared ready at $CF_BIN"
  else
    rm -f "$CF_BIN.tmp"
    echo "!! Could not pre-fetch cloudflared — app/main.py will retry this on boot" >>"$BACKEND_DIR/logs/install.err"
  fi
fi

echo "==> Install complete"
