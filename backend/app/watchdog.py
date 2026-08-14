"""Self-healing watchdog (TOOLS/Automation + TOOLS/Security).

Runs in the background and watches for conditions that would degrade Myra
(Cloudflare tunnel failure, connection issues, oversized logs/caches, max CPU,
disk pressure). When it detects a problem it:

1. Cleans everything that can be regenerated — logs, caches, temp files,
   uploaded junk, old screenshots — while NEVER touching:
     - the database (myra.db / user data)
     - Myra's memory (stored in the DB)
     - the agent workspace (user's files)
     - user sessions / auth
2. Restarts the process so Myra picks up from where it stopped (state lives
   in the DB + workspace, so it survives).

Triggers are read from environment so the panel owner can tune them.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from .config import settings

logger = logging.getLogger("myra.watchdog")

# Paths that are safe to delete on a cleanup run — regenerated on demand.
# Deliberately does NOT touch the user's workspace (uploaded files, screenshots,
# user projects) — only backend-level caches/logs/temp.
CLEANABLE = ("logs", "cache", "tmp", ".cache", "__pycache__", ".install-stamp", "bin/tunnel_url.txt")


def _dir_size(path: Path) -> int:
    """Total bytes under a path (0 if missing)."""
    if not path.exists():
        return 0
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _cpu_percent() -> float:
    """Best-effort current CPU usage (0-100)."""
    try:
        import psutil  # type: ignore

        return psutil.cpu_percent(interval=0.5)
    except Exception:
        # fallback: /proc/loadavg
        try:
            load = float(open("/proc/loadavg").read().split()[0])
            cpus = os.cpu_count() or 1
            return min(100.0, (load / cpus) * 100.0)
        except Exception:
            return 0.0


def _disk_usage_percent(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
        return (usage.used / usage.total) * 100.0
    except Exception:
        return 0.0


def _tunnel_healthy() -> bool:
    """Check the tunnel URL file exists and, if PUBLIC_API_URL is set, it responds.

    Hits the *local* origin, not the public hostname. Checking through the
    public Cloudflare-fronted URL means this shares fate with Cloudflare's
    edge (bot-protection challenges, transient 502/523s, DNS blips) — none
    of which mean the local API is actually unhealthy, but all of which used
    to trigger a full process restart here, killing every live SSE stream
    and in-flight agent run over nothing. Checking 127.0.0.1 directly tests
    the one thing this trigger should care about: is *this* process alive
    and answering.
    """
    try:
        url_file = Path(settings.base_dir) / ".bin" / "tunnel_url.txt"
        if not url_file.exists():
            return True  # not configured — nothing to check
        url = url_file.read_text().strip()
        if not url:
            return True
        if not settings.public_api_url:
            return True
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.port}/health",
            headers={"User-Agent": "Mozilla/5.0 (compatible; myra-watchdog)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


def _cleanup() -> dict[str, object]:
    """Delete cleanable paths under BASE_DIR + backend. Returns what was freed."""
    from .config import BASE_DIR, BACKEND_DIR

    freed = 0
    removed: list[str] = []
    for base in (BASE_DIR, BACKEND_DIR):
        for name in CLEANABLE:
            target = base / name
            if target.exists():
                size = _dir_size(target)
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    freed += size
                    removed.append(str(target))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not remove %s: %s", target, exc)
    return {"freedBytes": freed, "removed": removed}


def _restart() -> None:
    """Gracefully restart the current process so Myra resumes from saved state."""
    logger.info("Watchdog restarting Myra process")
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        sys.exit(0)


def _watch_once() -> dict[str, object]:
    """Evaluate all triggers; return what (if anything) fired."""
    fired: list[str] = []
    cpu = _cpu_percent()
    disk = _disk_usage_percent(settings.base_dir)
    cleanables_size = sum(
        _dir_size(Path(settings.base_dir) / name)
        for name in CLEANABLE
        if (Path(settings.base_dir) / name).exists()
    )

    if settings.watchdog_cpu_max and cpu >= settings.watchdog_cpu_max:
        fired.append(f"cpu={cpu:.0f}%")
    if settings.watchdog_disk_max and disk >= settings.watchdog_disk_max:
        fired.append(f"disk={disk:.0f}%")
    if settings.watchdog_cleanable_max and cleanables_size >= settings.watchdog_cleanable_max:
        fired.append(f"cleanable={cleanables_size // (1024 * 1024)}MB")
    if settings.watchdog_check_tunnel and not _tunnel_healthy():
        fired.append("tunnel-unhealthy")

    return {"fired": fired, "cpu": cpu, "disk": disk}


def start_watchdog() -> None:
    """Start the watchdog loop in a daemon thread."""
    if settings.watchdog_enabled is False or not settings.watchdog_enabled:
        logger.info("Watchdog disabled (MYRA_WATCHDOG_ENABLED=0)")
        return

    interval = max(30, settings.watchdog_interval_seconds)
    # Consecutive bad checks required before a restart fires. A restart kills
    # every live SSE stream and in-flight agent run, so one noisy reading
    # (a GC pause, a momentary CPU spike, a transient edge hiccup) must not
    # be able to trigger it on its own — only a sustained problem should.
    REQUIRED_STREAK = 3
    fail_streak = 0

    def loop() -> None:
        nonlocal fail_streak
        logger.info("Watchdog started (every %ss, needs %sx consecutive)", interval, REQUIRED_STREAK)
        while True:
            time.sleep(interval)
            try:
                result = _watch_once()
                if result["fired"]:
                    fail_streak += 1
                    logger.warning(
                        "Watchdog check failed (%s/%s): %s",
                        fail_streak,
                        REQUIRED_STREAK,
                        ", ".join(result["fired"]),
                    )
                    if fail_streak >= REQUIRED_STREAK:
                        logger.warning("Watchdog triggered after sustained failures — restarting")
                        _cleanup()
                        _restart()
                else:
                    fail_streak = 0
            except Exception:  # noqa: BLE001
                logger.exception("Watchdog iteration failed")

    threading.Thread(target=loop, daemon=True, name="myra-watchdog").start()
