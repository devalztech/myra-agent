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

        # --- registration allowlist ---------------------------------------
        # Comma-separated list of exact emails ("a@b.com") and/or bare
        # domains ("@company.com" or "company.com") permitted to register.
        # Empty (default) = open registration, unchanged from before.
        # Matching is case-insensitive; entries are normalised at parse time
        # so comparisons at request time are a plain set/suffix check.
        raw_allowed = _env("MYRA_ALLOWED_EMAILS") or _env("MYRA_EMAIL_ALLOWLIST")
        self.allowed_emails: set[str] = set()
        self.allowed_email_domains: set[str] = set()
        for entry in raw_allowed.split(","):
            entry = entry.strip().lower()
            if not entry:
                continue
            if entry.startswith("@"):
                self.allowed_email_domains.add(entry[1:])
            elif "@" in entry:
                self.allowed_emails.add(entry)
            else:
                # bare domain like "company.com" -> treat as @company.com
                self.allowed_email_domains.add(entry)
        self.registration_open = not (self.allowed_emails or self.allowed_email_domains)

        # --- database (SQLite only) ---------------------------------------
        self.sqlite_path = Path(_env("MYRA_SQLITE_PATH") or (BASE_DIR / "database" / "myra.db"))
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_url = f"sqlite:///{self.sqlite_path}"

        # --- generation --------------------------------------------------
        self.max_tokens = _env_int("MYRA_MAX_TOKENS", 512)
        self.temperature = _env_float("MYRA_TEMPERATURE", 0.4)
        self.top_p = _env_float("MYRA_TOP_P", 0.95)

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

        # Google Generative Language API (v1beta) — switchable provider.
        # Not keyless: Google always wants a key. Uses an optional
        # GEMINI_API_KEY (or GOOGLE_API_KEY). When absent the provider stays
        # visible and returns a clear "set GEMINI_API_KEY" message.
        self.gemini_api_key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
        self.gemini_base_url = _env(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        )
        self.gemini_model = _env("GEMINI_MODEL", "gemini-2.5-flash")

        # --- free remote OpenAI-compatible providers -----------------------
        # Groq — highest free tier (llama-3.1-8b-instant: ~14.4k req/day,
        # 500k tokens/day, no card).
        self.groq_api_key = _env("GROQ_API_KEY")
        self.groq_base_url = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        self.groq_model = _env("GROQ_MODEL", "llama-3.1-8b-instant")

        # SambaNova Cloud — free hosted open models (DeepSeek, Llama, MiniMax).
        self.sambanova_api_key = _env("SAMBANOVA_API_KEY")
        self.sambanova_base_url = _env(
            "SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1"
        )
        self.sambanova_model = _env("SAMBANOVA_MODEL", "Meta-Llama-3.3-70B-Instruct")

        # Scaleway Generative APIs — free hosted open models.
        self.scaleway_api_key = _env("SCALEWAY_API_KEY")
        self.scaleway_base_url = _env("SCALEWAY_BASE_URL", "https://api.scaleway.ai/v1")
        self.scaleway_model = _env("SCALEWAY_MODEL", "llama-3.3-70b-instruct")

        # Pollinations — fully keyless OpenAI-compatible endpoint. Verified
        # working anonymously (no API key) via its legacy text API.
        self.pollinations_base_url = _env(
            "POLLINATIONS_BASE_URL", "https://text.pollinations.ai/openai"
        )
        self.pollinations_model = _env("POLLINATIONS_MODEL", "openai")

        # --- agent workspace ---------------------------------------------
        # Myra's OWN working directory: /home/container/myra by default —
        # a dedicated subfolder INSIDE the Pterodactyl server directory
        # (rather than a sibling dir). Every filesystem / terminal tool is
        # still hard-jailed to this subfolder via safe_path(); the rest of
        # /home/container stays in protected_paths below, so Myra can
        # still never read or write anything outside its own subfolder
        # even though that subfolder now lives inside the panel's tree.
        self.workspace_dir = Path(
            _env("MYRA_WORKSPACE_DIR") or (Path.home() / "myra")
        ).expanduser()
        # Paths Myra may never read or write, even if something escapes above.
        self.protected_paths = [
            p.strip()
            for p in _env(
                "MYRA_PROTECTED_PATHS",
                "/etc,/root/.ssh,/proc,/sys,/var/lib/pterodactyl",
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

        # --- self-healing watchdog -----------------------------------------
        # Monitors tunnel health, CPU, disk and cleanable-file size. On a
        # trigger it clears logs/caches/temp/uploads (NEVER the db, memory,
        # workspace or user sessions) then restarts the process so Myra
        # resumes from saved state. 0 disables a trigger.
        self.watchdog_enabled = _env_bool("MYRA_WATCHDOG_ENABLED", True)
        self.watchdog_interval_seconds = _env_int("MYRA_WATCHDOG_INTERVAL", 60)
        self.watchdog_cpu_max = _env_int("MYRA_WATCHDOG_CPU_MAX", 0)  # 0 = off
        self.watchdog_disk_max = _env_int("MYRA_WATCHDOG_DISK_MAX", 0)  # 0 = off
        self.watchdog_cleanable_max = _env_int(
            "MYRA_WATCHDOG_CLEANABLE_MAX_MB", 0
        ) * 1024 * 1024  # bytes; 0 = off
        self.watchdog_check_tunnel = _env_bool("MYRA_WATCHDOG_CHECK_TUNNEL", True)
        # The deploy root (BASE_DIR) is the watchdog's home for sizing.
        self.base_dir = BASE_DIR

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


    @property
    def is_sqlite(self) -> bool:
        # Kept as a property (rather than deleted outright) so main.py's
        # boot log / health payload and any other call site don't need to
        # change now that SQLite is the only backend — it always returns
        # True, but the call sites stay honest about what they're checking.
        return True

    def is_email_allowed(self, email: str) -> bool:
        """True if registration is open, or the email clears the allowlist."""
        if self.registration_open:
            return True
        email = (email or "").strip().lower()
        if email in self.allowed_emails:
            return True
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        return domain in self.allowed_email_domains


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
