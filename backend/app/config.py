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
        self.max_tokens = _env_int("MYRA_MAX_TOKENS", 512)
        self.temperature = _env_float("MYRA_TEMPERATURE", 0.4)
        self.top_p = _env_float("MYRA_TOP_P", 0.95)
        self.gpu_layers = _env_int("MYRA_GPU_LAYERS", 0)
        # Thread count. llama.cpp scales NEGATIVELY once you ask for more
        # threads than the container is actually allowed to run: on a panel
        # limited to 200% CPU (2 cores) sitting on a 64-core host,
        # os.cpu_count() / sched_getaffinity() both report 64, so llama.cpp
        # spawns 64 workers that the cgroup then throttles — the same 3B
        # model measured 5.4 tok/s at the right thread count and 0.25 tok/s
        # oversubscribed. _auto_threads() now reads the cgroup CPU quota so
        # the default matches the real allowance.
        self.threads = _env_int("MYRA_THREADS", 0) or self._auto_threads()
        # Prompt-ingest batch. Bigger = faster prefill, more RAM. On a
        # 1-2 core panel a huge batch just costs RAM, so scale it with the
        # thread count instead of always using 512.
        self.batch_size = _env_int("MYRA_BATCH_SIZE", 0) or (256 if self.threads <= 2 else 512)
        # Explicit tier override ("nano" | "compact" | "standard" | ...).
        self.model_tier = _env("MYRA_MODEL_TIER").lower()
        # Never auto-select a model bigger than this many GB of weights.
        # Huge models fit in RAM but are unusably slow on CPU-only panels.
        self.max_auto_model_gb = _env_float("MYRA_MAX_AUTO_MODEL_GB", 2.5)
        # Reserve RAM (GB) for the OS/API when picking a model tier.
        self.ram_reserve_gb = _env_float("MYRA_RAM_RESERVE_GB", 1.0)
        self.ram_override_gb = _env_float("MYRA_RAM_GB", 0.0)
        # Preload the model at boot (in a background thread) so the first
        # chat message isn't stuck behind a cold model load.
        self.preload_model = _env_bool("MYRA_PRELOAD_MODEL", True)
        # Keep the model resident between requests (mmap on, mlock off by
        # default so a small panel can still page weights out under pressure).
        self.use_mmap = _env_bool("MYRA_USE_MMAP", True)
        self.use_mlock = _env_bool("MYRA_USE_MLOCK", False)

        self.history_window = _env_int("MYRA_HISTORY_WINDOW", 20)

        # --- providers ----------------------------------------------------
        # Default provider for new users: "local" (llama.cpp GGUF) or "agnes".
        self.default_provider = _env("MYRA_DEFAULT_PROVIDER", "local").lower()
        self.agnes_api_key = _env("AGNES_API_KEY")
        # The real Agnes API host is apihub.agnes-ai.com, not api.agnes.ai —
        # the old default pointed at a domain that isn't the documented
        # endpoint, so every request would fail unless AGNES_BASE_URL was
        # set explicitly. agnes-2.0-flash is the documented default model.
        self.agnes_base_url = _env("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
        self.agnes_model = _env("AGNES_MODEL", "agnes-2.0-flash")

        # --- agent workspace ---------------------------------------------
        # Myra's OWN working directory. It lives OUTSIDE the Pterodactyl
        # server files by default (a sibling directory), and every filesystem
        # / terminal tool is hard-jailed to it. See app/workspace.py.
        self.workspace_dir = Path(
            _env("MYRA_WORKSPACE_DIR") or (Path.home() / "myra-workspace")
        ).expanduser()
        # Paths Myra may never read or write, even if something escapes above.
        self.protected_paths = [
            p.strip()
            for p in _env(
                "MYRA_PROTECTED_PATHS",
                "/home/container,/etc,/root/.ssh,/proc,/sys,/var/lib/pterodactyl",
            ).split(",")
            if p.strip()
        ]

        # --- agent limits -------------------------------------------------
        self.max_tool_calls = _env_int("MYRA_MAX_TOOL_CALLS", 24)
        self.max_agent_steps = _env_int("MYRA_MAX_AGENT_STEPS", 16)
        self.tool_timeout_seconds = _env_int("MYRA_TOOL_TIMEOUT", 120)
        self.agent_timeout_seconds = _env_int("MYRA_AGENT_TIMEOUT", 900)
        self.max_file_bytes = _env_int("MYRA_MAX_FILE_BYTES", 512_000)
        self.max_output_chars = _env_int("MYRA_MAX_OUTPUT_CHARS", 12_000)
        self.max_upload_bytes = _env_int("MYRA_MAX_UPLOAD_BYTES", 25_000_000)
        # Commands requiring explicit human approval before they run.
        self.approval_required = _env_bool("MYRA_APPROVAL_REQUIRED", False)
        self.enable_network_tools = _env_bool("MYRA_ENABLE_NETWORK_TOOLS", True)
        self.enable_browser_tools = _env_bool("MYRA_ENABLE_BROWSER_TOOLS", True)
        self.scheduler_enabled = _env_bool("MYRA_SCHEDULER_ENABLED", True)
        self.scheduler_interval_seconds = _env_int("MYRA_SCHEDULER_INTERVAL", 30)

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

    @staticmethod
    def _cgroup_cpu_quota() -> float | None:
        """CPU cores this container may actually use, from the cgroup.

        Pterodactyl/HidenCloud express the limit as a percentage (200% = 2
        cores) which lands in the kernel as a cfs quota/period pair.
        """
        # cgroup v2: "<quota> <period>" or "max <period>"
        try:
            raw = Path("/sys/fs/cgroup/cpu.max").read_text().strip().split()
            if len(raw) == 2 and raw[0] != "max":
                quota, period = float(raw[0]), float(raw[1])
                if quota > 0 and period > 0:
                    return quota / period
        except (OSError, ValueError):
            pass
        # cgroup v1
        try:
            quota = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
            period = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
            if quota > 0 and period > 0:
                return quota / period
        except (OSError, ValueError):
            pass
        return None

    @classmethod
    def _auto_threads(cls) -> int:
        try:
            available = float(len(os.sched_getaffinity(0)))
        except AttributeError:
            available = float(os.cpu_count() or 4)

        quota = cls._cgroup_cpu_quota()
        if quota and quota > 0:
            available = min(available, quota)

        # Round down (2.0 cores -> 2 threads); never below 1, never above 16
        # because llama.cpp stops scaling on shared/virtualised CPUs there.
        return max(1, min(int(available), 16))


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
