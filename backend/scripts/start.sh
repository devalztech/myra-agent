#!/usr/bin/env bash
# Pterodactyl startup command:
#   bash backend/scripts/start.sh
#
# Boot sequence: install deps -> install cloudflared -> download model
#                -> start tunnel -> start the API.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKEND_DIR="$(pwd)"

# Load a .env file if the panel provides one.
if [[ -f "$BACKEND_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$BACKEND_DIR/.env"
  set +a
fi

PY="${PYTHON_BIN:-python3}"
PORT="${SERVER_PORT:-${PORT:-8000}}"
HOST="${HOST:-0.0.0.0}"
export PORT HOST

echo "###################################################################"
echo "#  Myra Agent backend"
echo "#  host=$HOST port=$PORT"
echo "#  db=${DATABASE_URL:-sqlite (database/myra.db)}"
echo "###################################################################"

bash scripts/install.sh

if [[ "${MYRA_SKIP_TUNNEL:-0}" != "1" ]]; then
  bash scripts/tunnel.sh || true
fi

cleanup() {
  if [[ -f "$BACKEND_DIR/.tunnel-pid" ]]; then
    kill "$(cat "$BACKEND_DIR/.tunnel-pid")" 2>/dev/null || true
    rm -f "$BACKEND_DIR/.tunnel-pid"
  fi
}
trap cleanup EXIT INT TERM

echo "==> Starting API on $HOST:$PORT"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1 --timeout-keep-alive 75
