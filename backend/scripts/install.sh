#!/usr/bin/env bash
# Installs Python dependencies + cloudflared. Safe to re-run on every boot.
#
# The core requirements never include llama-cpp-python: if the local model
# isn't needed (a remote provider is the default) or the panel egg can't
# build it, Myra still boots and runs fine against Groq/SambaNova/Scaleway/
# Pollinations. The local engine is installed as an OPTIONAL extra only when
# explicitly requested.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKEND_DIR="$(pwd)"

PY="${PYTHON_BIN:-python3}"
PIP_FLAGS="--no-cache-dir --disable-pip-version-check"
STAMP=".install-stamp"
REQ_HASH="$("$PY" - <<'EOF'
import hashlib, pathlib
h = hashlib.sha256()
for p in ("requirements.txt", "requirements-local.txt"):
    h.update(pathlib.Path(p).read_bytes())
print(h.hexdigest())
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

# ---- local model (optional) ----------------------------------------------
# Install llama-cpp only when the user actually wants local inference AND the
# egg can build it. Everything else (remote providers, mock) needs no model.
WANT_LOCAL=0
if [[ "${MYRA_LLM_BACKEND:-llama_cpp}" != "mock" && "${MYRA_SKIP_MODEL:-0}" != "1" ]]; then
  # Only want local if no remote provider is the default.
  case "${MYRA_DEFAULT_PROVIDER:-local}" in
    local|mock|"") WANT_LOCAL=1 ;;
    *) echo "==> Default provider is remote (${MYRA_DEFAULT_PROVIDER}); skipping local model" ;;
  esac
fi

if [[ "$WANT_LOCAL" == "1" ]]; then
  echo "==> Installing optional local-inference engine (llama-cpp-python)"
  if "$PY" -m pip install $PIP_FLAGS \
      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
      -r requirements-local.txt; then
    echo "==> llama-cpp installed OK"
  else
    echo "!! llama-cpp failed to install — Myra will use remote providers instead."
    echo "   Set MYRA_LLM_BACKEND=mock and MYRA_DEFAULT_PROVIDER=<remote> to run without it."
  fi
else
  echo "==> Skipping local inference engine"
fi

# ---- model download -------------------------------------------------------
if [[ "${MYRA_SKIP_MODEL:-0}" == "1" || "${MYRA_LLM_BACKEND:-llama_cpp}" == "mock" || "$WANT_LOCAL" != "1" ]]; then
  echo "==> Skipping model download"
else
  "$PY" scripts/download_model.py || echo "!! Model download failed — it will retry on first chat"
fi

echo "==> Install complete"
