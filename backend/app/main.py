"""Myra Agent backend — FastAPI application entry point.

Chat-only scope for now (no task execution / coding tools).
Inference is 100% local (llama.cpp + GGUF Llama models).
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Allow this file to be run two ways:
#   1) as a module/package (uvicorn app.main:app, python -m app.main) -- normal case
#   2) as a bare script (python3 app/main.py) -- some panel "eggs" hardcode this
# In case (2), relative imports below (`from .config import ...`) fail with
# "attempted relative import with no known parent package" because Python
# doesn't know `app` is a package when the file is executed directly. This
# shim detects that situation and puts the project root on sys.path so plain
# `app.xxx` imports work instead.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "app"

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importing app.config is what triggers .env to be loaded (see the top of
# config.py) — everything after this line can rely on os.environ / settings
# already reflecting .env, regardless of how this file was invoked.
from app.config import settings
from app.database import init_db
from app.llm.engine import get_engine
from app.llm.tiers import detect_total_ram_gb, select_tier
from app.routers import auth, chat, sessions

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("myra")


def _tunnel_url_file() -> Path:
    from app.config import BASE_DIR

    return BASE_DIR / ".bin" / "tunnel_url.txt"


def _start_cloudflare_tunnel(port: int) -> None:
    """Run a Cloudflare tunnel in a background daemon thread.

    With CLOUDFLARE_TUNNEL_TOKEN set (in .env or as a real panel env var):
    runs a *named* tunnel, giving a STABLE public hostname configured once in
    the Cloudflare dashboard under Public Hostname. This survives restarts —
    unlike the old Quick Tunnel (--url flag, no token), which gets a new
    random *.trycloudflare.com URL every single boot.

    Everything happens in-process here (no scripts/tunnel.sh subprocess, no
    shell env-var handoff) so the token — read via `settings`, which loads
    .env itself in config.py — is never at risk of not reaching the process
    that actually needs it. `settings` also means this works whether the
    token was set as a real HidenCloud panel variable or only lives in
    .env, instead of silently falling back to a quick tunnel in the
    .env-only case.

    Falls back to a random Quick Tunnel if no token is set, so this doesn't
    break anything for anyone who hasn't configured a named tunnel yet.
    """
    import platform
    import stat
    import subprocess
    import threading
    import time
    import urllib.error
    import urllib.request

    if settings.skip_tunnel:
        logger.info("MYRA_SKIP_TUNNEL set — skipping Cloudflare tunnel")
        return

    bin_dir = Path(__file__).resolve().parent.parent / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cloudflared_path = bin_dir / "cloudflared"
    url_file = _tunnel_url_file()

    if not cloudflared_path.exists():
        machine = platform.machine().lower()
        arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
        logger.info("Downloading cloudflared (%s)...", arch)
        try:
            urllib.request.urlretrieve(url, cloudflared_path)
            st = os.stat(cloudflared_path)
            os.chmod(cloudflared_path, st.st_mode | stat.S_IEXEC)
        except Exception:
            logger.exception("Failed to download cloudflared — public tunnel will not start")
            return

    token = settings.cloudflare_tunnel_token.strip()

    # Always address the local origin as 127.0.0.1, never "localhost".
    # "localhost" resolves to ::1 (IPv6) first on most Linux images, but
    # uvicorn bound to 0.0.0.0 only listens on IPv4 — so cloudflared dials
    # [::1]:PORT, gets connection-refused, and Cloudflare returns a
    # "Bad gateway" (error 502) even though the tunnel itself is connected
    # and the API answers fine on 127.0.0.1. This was THE cause of the 502.
    local_origin = f"http://127.0.0.1:{port}"

    if token:
        # --url overrides the ingress service that comes down from the
        # Cloudflare dashboard for this token. Dashboard-created tunnels are
        # almost always configured as "http://localhost:PORT", which is the
        # IPv6 trap described above; the override pins the origin to IPv4
        # loopback on the port this process is actually serving. It also
        # makes the tunnel immune to a stale/incorrect port in the dashboard
        # config (e.g. panel-assigned SERVER_PORT != 8000).
        cmd = [
            str(cloudflared_path),
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token",
            token,
            "--url",
            local_origin,
        ]
        stable_url = settings.public_api_url.strip()
        if stable_url:
            if not stable_url.startswith(("http://", "https://")):
                stable_url = f"https://{stable_url}"
            try:
                url_file.write_text(stable_url)
            except Exception:
                logger.exception("Failed to write tunnel_url.txt")
        logger.info(
            "Starting named Cloudflare tunnel (stable hostname), origin %s", local_origin
        )
    else:
        cmd = [str(cloudflared_path), "tunnel", "--url", local_origin]
        logger.warning(
            "CLOUDFLARE_TUNNEL_TOKEN not set — falling back to a random Quick "
            "Tunnel URL that changes every restart. Set CLOUDFLARE_TUNNEL_TOKEN "
            "and PUBLIC_API_URL in .env for a permanent URL."
        )

    def _wait_for_local_api(timeout: float = 30.0) -> None:
        """Don't expose the tunnel before the API can actually answer.

        The tunnel thread starts from the lifespan hook, which runs before
        uvicorn accepts connections. Connecting cloudflared first meant the
        very first requests through the hostname could still 502 for a few
        seconds after boot.
        """
        import socket

        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    logger.info("Local API is accepting connections on %s", local_origin)
                    return
            time.sleep(0.5)
        logger.warning(
            "Local API not reachable on %s yet — starting tunnel anyway", local_origin
        )

    def _run_forever() -> None:
        _wait_for_local_api()
        while True:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:  # type: ignore[union-attr]
                    line = line.strip()
                    if "trycloudflare.com" in line:
                        logger.info("Public tunnel URL: %s", line)
                        for word in line.split():
                            if "trycloudflare.com" in word:
                                try:
                                    url_file.write_text(word)
                                except Exception:
                                    logger.exception("Failed to write tunnel_url.txt")
                                break
                    else:
                        logger.info("[cloudflared] %s", line)
                proc.wait()
                logger.warning("cloudflared exited — restarting tunnel in 5s")
            except Exception:
                logger.exception("cloudflared process failed — retrying in 5s")
            time.sleep(5)

    def _verify_public_hostname() -> None:
        """Check the public hostname actually reaches THIS process.

        A named tunnel can be fully "connected" while the public hostname
        still returns Cloudflare's 502 Bad gateway page, because the edge
        never routes the request to the connector. That happens when the DNS
        record for the hostname is a proxied A/AAAA record instead of a CNAME
        to <tunnel-id>.cfargotunnel.com. cloudflared logs nothing in that case
        (it sees zero requests), which makes it look like an app bug. This
        check turns that silent failure into an actionable log line.
        """
        target = settings.public_api_url.strip()
        if not target:
            return
        if not target.startswith(("http://", "https://")):
            target = f"https://{target}"
        time.sleep(12)  # let the tunnel register and DNS settle
        try:
            req = urllib.request.Request(
                f"{target.rstrip('/')}/health",
                method="GET",
                # Cloudflare's bot protection answers 403 to header-less
                # clients, which would muddy the diagnosis below.
                headers={"User-Agent": "Mozilla/5.0 (compatible; myra-tunnel-selfcheck)"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    logger.info("Public hostname verified: %s -> this process", target)
                    return
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception as exc:
            logger.warning("Could not verify public hostname %s: %s", target, exc)
            return

        if code in (502, 503, 504):
            logger.error(
                "Public hostname %s returned HTTP %s (Cloudflare 'Bad gateway') while "
                "the tunnel is connected. The tunnel is fine — the DNS record for that "
                "hostname is not pointing at this tunnel. Fix it in the Cloudflare "
                "dashboard: delete any proxied A/AAAA record for the hostname, then in "
                "Zero Trust > Networks > Tunnels > (your tunnel) > Public Hostname add "
                "the hostname with service %s. That creates the required CNAME to "
                "<tunnel-id>.cfargotunnel.com.",
                target,
                code,
                local_origin,
            )
        else:
            logger.warning("Public hostname %s returned HTTP %s", target, code)

    threading.Thread(target=_run_forever, daemon=True).start()
    if token:
        threading.Thread(target=_verify_public_hostname, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    ram = detect_total_ram_gb()
    tier = select_tier(ram)
    logger.info(
        "Database: %s", "sqlite (%s)" % settings.sqlite_path if settings.is_sqlite else "postgresql"
    )
    logger.info("Detected RAM: %.2f GB -> tier '%s' (%s)", ram, tier.name, tier.description)
    _start_cloudflare_tunnel(settings.port)
    if settings.preload_model:
        # Load in a daemon thread so boot (and /health) never blocks on a
        # multi-GB model load or download, while still making sure the first
        # real chat message doesn't pay the cold-start cost.
        import threading as _threading

        def _preload() -> None:
            try:
                engine = get_engine()
                loader = getattr(engine, "load", None)
                if callable(loader):
                    import time as _time

                    started = _time.time()
                    loader()
                    logger.info("Model preloaded in %.1fs", _time.time() - started)
            except Exception as exc:  # pragma: no cover
                logger.warning("Model preload skipped: %s", exc)

        _threading.Thread(target=_preload, daemon=True, name="myra-preload").start()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=None if "*" in settings.cors_origins else r"https://.*\.trycloudflare\.com",
    allow_credentials="*" not in settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(chat.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    tunnel_url = None
    try:
        tunnel_url = _tunnel_url_file().read_text().strip() or None
    except FileNotFoundError:
        pass
    return {
        "status": "ok",
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "llm_backend": settings.llm_backend,
        "tunnel_url": tunnel_url,
    }


@app.get("/", tags=["health"])
def root() -> dict:
    return {"name": settings.app_name, "docs": "/docs", "health": "/health"}


if __name__ == "__main__":
    # Runs only when the panel executes this file directly
    # (e.g. `python3 app/main.py`) instead of `uvicorn app.main:app`.
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", settings.host),
        port=settings.port,
    )
