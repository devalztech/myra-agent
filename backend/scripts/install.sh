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

# ---- cloudflared ---------------------------------------------------------
install_cloudflared() {
  local bin_dir="$BACKEND_DIR/bin"
  mkdir -p "$bin_dir"
  if command -v cloudflared >/dev/null 2>&1; then
    echo "==> cloudflared already on PATH: $(command -v cloudflared)"
    return 0
  fi
  if [[ -x "$bin_dir/cloudflared" ]]; then
    echo "==> cloudflared already installed at $bin_dir/cloudflared"
    return 0
  fi
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    armv7l) arch="arm" ;;
    *) echo "!! Unsupported arch $arch for cloudflared, skipping"; return 0 ;;
  esac
  local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
  echo "==> Downloading cloudflared ($arch)"
  if curl -fsSL "$url" -o "$bin_dir/cloudflared.tmp"; then
    chmod +x "$bin_dir/cloudflared.tmp"
    mv "$bin_dir/cloudflared.tmp" "$bin_dir/cloudflared"
    echo "==> cloudflared installed at $bin_dir/cloudflared"
  else
    rm -f "$bin_dir/cloudflared.tmp"
    echo "!! cloudflared download failed — the API will still start without a tunnel"
  fi
}

if [[ "${MYRA_SKIP_TUNNEL:-0}" == "1" ]]; then
  echo "==> MYRA_SKIP_TUNNEL=1, skipping cloudflared install"
else
  install_cloudflared
fi

# ---- model ---------------------------------------------------------------
if [[ "${MYRA_SKIP_MODEL:-0}" == "1" || "${MYRA_LLM_BACKEND:-llama_cpp}" == "mock" ]]; then
  echo "==> Skipping model download"
else
  "$PY" scripts/download_model.py || echo "!! Model download failed — it will retry on first chat"
fi

echo "==> Install complete"
