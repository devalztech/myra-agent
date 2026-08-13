"""Central configuration for the Myra backend.

Every value is driven by environment variables so the same image can run on a
Pterodactyl panel, plain Docker, or a local machine.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]  # the folder containing app/
# Repo root: if this backend lives inside a "backend/" subfolder (local dev
# layout, e.g. repo/backend/app/config.py), step up one more; if app/ sits
# directly at the deploy root (e.g. Pterodactyl's /home/container/app/config.py),
# treat that same folder as the root instead of walking past it.
BASE_DIR = BACKEND_DIR.parent if BACKEND_DIR.name == "backend" else BACKEND_DIR

# Load .env directly here, before any _env()/os.environ reads happen below.
# This used to be done in main.py via load_dotenv(), but that only works
# reliably when Settings() is constructed *after* main.py's load_dotenv()
# call runs, and when nothing downstream (like a spawned subprocess) reads
# the token before that happens. Loading it here instead — at the top of
# config.py, before Settings.__init__ ever runs — means every value in this
# file (including the tunnel token below) is available at the exact moment
# it's first read, regardless of import order or whether this module is
# reached via `uvicorn app.main:app`, `python -m app.main`, or a bare
# `python app/main.py`. python-dotenv's load_dotenv() does not override
# variables the panel already set as real env vars, so panel-level env vars
# still take priority over .env — .env only fills in what's missing.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover - python-dotenv is in requirements.txt
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class Settings:
    """Runtime settings, resolved once per process."""

    def __init__(self) -> None:
        # --- app ---------------------------------------------------------
        self.app_name = "Myra Agent API"
        self.host = _env("HOST", "0.0.0.0")
        # Pterodactyl injects SERVER_PORT; PORT is the common Docker convention.
        self.port = _env_int("SERVER_PORT", 0) or _env_int("PORT", 8000)
        self.debug = _env_bool("MYRA_DEBUG", False)

        # --- auth --------------------------------------------------------
        self.jwt_secret = _env("MYRA_JWT_SECRET") or _env("JWT_SECRET") or "myra-dev-secret-change-me"
        self.jwt_algorithm = "HS256"
        self.jwt_expire_minutes = _env_int("MYRA_JWT_EXPIRE_MINUTES", 60 * 24 * 7)

        # --- cors --------------------------------------------------------
        raw_origins = _env("MYRA_CORS_ORIGINS", "*")
        self.cors_origins = [o.strip() for o in raw_origins.split(",") if o.strip()] or ["*"]

        # --- database ----------------------------------------------------
        # PostgreSQL when DATABASE_URL (or the discrete POSTGRES_* vars) is set,
        # otherwise a SQLite file at <repo>/database/myra.db
        self.sqlite_path = Path(_env("MYRA_SQLITE_PATH") or (BASE_DIR / "database" / "myra.db"))
        self.database_url = self._resolve_database_url()

        # --- llm ---------------------------------------------------------
        # "llama_cpp" (default, local GGUF) or "mock" (deterministic, for tests)
        self.llm_backend = _env("MYRA_LLM_BACKEND", "llama_cpp").lower()
        self.models_dir = Path(_env("MYRA_MODELS_DIR") or (BACKEND_DIR / "models"))
        self.model_path = _env("MYRA_MODEL_PATH")  # explicit .gguf file wins
        self.model_repo = _env("MYRA_MODEL_REPO")  # HF repo id override
        self.model_file = _env("MYRA_MODEL_FILE")  # HF filename override
        self.context_size = _env_int("MYRA_CONTEXT_SIZE", 0)  # 0 -> auto from tier
        self.max_tokens = _env_int("MYRA_MAX_TOKENS", 1024)
        self.temperature = _env_float("MYRA_TEMPERATURE", 0.4)
        self.top_p = _env_float("MYRA_TOP_P", 0.95)
        self.gpu_layers = _env_int("MYRA_GPU_LAYERS", 0)
        self.threads = _env_int("MYRA_THREADS", 0) or (os.cpu_count() or 4)
        # Reserve RAM (GB) for the OS/API when picking a model tier.
        self.ram_reserve_gb = _env_float("MYRA_RAM_RESERVE_GB", 1.5)
        self.ram_override_gb = _env_float("MYRA_RAM_GB", 0.0)
        self.preload_model = _env_bool("MYRA_PRELOAD_MODEL", False)

        self.history_window = _env_int("MYRA_HISTORY_WINDOW", 20)

        # --- cloudflare tunnel ---------------------------------------------
        # With CLOUDFLARE_TUNNEL_TOKEN set: named tunnel, stable hostname
        # (configured once in the Cloudflare dashboard's Public Hostname
        # settings). Without one: falls back to a random *.trycloudflare.com
        # Quick Tunnel URL that changes on every restart.
        self.cloudflare_tunnel_token = _env("CLOUDFLARE_TUNNEL_TOKEN")
        # Optional — only used to pre-fill .bin/tunnel_url.txt when a named
        # tunnel is active, so /health can report the URL immediately on
        # boot instead of waiting for the first log line from cloudflared.
        self.public_api_url = _env("PUBLIC_API_URL")
        self.skip_tunnel = _env_bool("MYRA_SKIP_TUNNEL", False)

    def _resolve_database_url(self) -> str:
        url = _env("DATABASE_URL") or _env("MYRA_DATABASE_URL")
        if not url:
            host = _env("POSTGRES_HOST")
            if host:
                user = _env("POSTGRES_USER", "myra")
                password = _env("POSTGRES_PASSWORD", "")
                db = _env("POSTGRES_DB", "myra")
                port = _env_int("POSTGRES_PORT", 5432)
                auth = f"{user}:{password}" if password else user
                url = f"postgresql+psycopg://{auth}@{host}:{port}/{db}"
        if url:
            # Normalise legacy schemes onto the psycopg (v3) driver.
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+psycopg://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg://", 1)
            return url

        # SQLite fallback.
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.sqlite_path}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
