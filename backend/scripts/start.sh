#!/usr/bin/env bash
# Pterodactyl startup command:
#   bash backend/scripts/start.sh
#
# Boot sequence: install deps -> download model -> start the API.
# The Cloudflare tunnel is started in-process by app/main.py itself
# (see _start_cloudflare_tunnel there) so it runs the same way no matter
# how the app is launched — this script, `uvicorn app.main:app` directly,
# or a panel that runs `python app/main.py` as a bare script. It's no
# longer started here to avoid launching it twice.
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

echo "==> Starting API on $HOST:$PORT"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1 --timeout-keep-alive 75
