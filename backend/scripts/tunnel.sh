#!/usr/bin/env bash
# Starts a Cloudflare tunnel in front of the local API.
#
#   CLOUDFLARE_TUNNEL_TOKEN set  -> named tunnel (stable hostname you configured)
#   token not set                -> quick tunnel with a random *.trycloudflare.com URL
#
# The resolved public URL is written to .tunnel-url so it can be pasted into the
# frontend's VITE_API_URL.
set -uo pipefail

cd "$(dirname "$0")/.."
BACKEND_DIR="$(pwd)"
PORT="${SERVER_PORT:-${PORT:-8000}}"
LOG="$BACKEND_DIR/logs/cloudflared.log"
URL_FILE="$BACKEND_DIR/.tunnel-url"
mkdir -p "$BACKEND_DIR/logs"
: > "$LOG"
rm -f "$URL_FILE"

CF_BIN="$(command -v cloudflared || true)"
[[ -z "$CF_BIN" && -x "$BACKEND_DIR/bin/cloudflared" ]] && CF_BIN="$BACKEND_DIR/bin/cloudflared"

if [[ -z "$CF_BIN" ]]; then
  echo "[tunnel] cloudflared not installed — skipping tunnel"
  exit 0
fi

if [[ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
  echo "[tunnel] Starting named Cloudflare tunnel (token provided)"
  "$CF_BIN" tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" >>"$LOG" 2>&1 &
  echo $! > "$BACKEND_DIR/.tunnel-pid"
  if [[ -n "${CLOUDFLARE_TUNNEL_HOSTNAME:-}" ]]; then
    echo "https://${CLOUDFLARE_TUNNEL_HOSTNAME}" > "$URL_FILE"
    echo "[tunnel] Public URL: https://${CLOUDFLARE_TUNNEL_HOSTNAME}"
  else
    echo "[tunnel] Named tunnel started. Public hostname is the one bound in your Cloudflare dashboard."
  fi
  exit 0
fi

echo "[tunnel] No CLOUDFLARE_TUNNEL_TOKEN — starting quick tunnel (random URL)"
"$CF_BIN" tunnel --no-autoupdate --url "http://localhost:${PORT}" >>"$LOG" 2>&1 &
echo $! > "$BACKEND_DIR/.tunnel-pid"

for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  if [[ -n "$URL" ]]; then
    echo "$URL" > "$URL_FILE"
    echo "==================================================================="
    echo "[tunnel] Public API URL: $URL"
    echo "[tunnel] Set this as VITE_API_URL in the frontend."
    echo "==================================================================="
    exit 0
  fi
  sleep 1
done

echo "[tunnel] Could not resolve a quick tunnel URL — see $LOG"
exit 0
