"""Self-healing Playwright/chromium install (TOOLS/Browser).

Myra's sandbox is small (about 1GB RAM, 14GB disk, shared CPU), so this is
deliberately conservative: it downloads chromium only, never `--with-deps`
(that pulls in apt system packages, needs root, and isn't safe or available
on a Pterodactyl container), checks free disk first, and only ever attempts
the install once per boot (or once per FAILURE_RETRY_SECONDS) so a broken
install doesn't get retried on every single tool call.

Both `app.services.browser` (the persistent `browser` tool) and
`app.agent.tools._screenshot_file` (`screenshot_file`) call
`ensure_chromium()` before touching Playwright, so the fix lives in one
place.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("myra.browser_setup")

# chromium (headless_shell) is roughly 130-150MB to download; give Playwright
# room to unpack + cache without getting anywhere near a 14GB disk's edge.
MIN_FREE_DISK_BYTES = 500 * 1024 * 1024  # 500MB headroom required
INSTALL_TIMEOUT_SECONDS = 240  # generous but bounded — a stuck download must not hang a tool call forever
FAILURE_RETRY_SECONDS = 15 * 60  # don't hammer a broken install every call; retry at most every 15 min

_lock = threading.Lock()
_last_attempt: float = 0.0
_last_result: str | None = None  # None = never tried, "" = success, else error string


def _chromium_installed() -> bool:
    """True if Playwright can actually find a chromium executable on disk.

    Deliberately does NOT just check `import playwright` — the Python
    package can be installed while the browser binary itself is still
    missing (this is exactly the "Executable doesn't exist at
    .../chromium_headless_shell.../headless_shell" error), which is the gap
    that let this fail silently before.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        return bool(path) and Path(path).exists()
    except Exception:
        return False


def _free_disk_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path if path.exists() else path.parent).free
    except Exception:
        return 0


def ensure_chromium() -> tuple[bool, str]:
    """Make sure Playwright + chromium are usable, installing if needed.

    Returns (ok, message). Safe to call from any tool right before it needs
    a browser — cheap when already installed, self-limiting when not.
    """
    if _chromium_installed():
        return True, "chromium already installed"

    with _lock:
        # Re-check inside the lock in case another thread just finished.
        if _chromium_installed():
            return True, "chromium already installed"

        global _last_attempt, _last_result
        now = time.monotonic()
        if _last_result is not None and (now - _last_attempt) < FAILURE_RETRY_SECONDS:
            wait = int(FAILURE_RETRY_SECONDS - (now - _last_attempt))
            return False, (
                f"Chromium install failed earlier this boot ({_last_result}). "
                f"Not retrying for another ~{wait}s to avoid burning the sandbox's "
                "CPU/disk on a broken install — ask the user to check hosting resources "
                "if this keeps happening."
            )

        _last_attempt = now

        free = _free_disk_bytes(Path.home())
        if free < MIN_FREE_DISK_BYTES:
            _last_result = f"only {free // (1024 * 1024)}MB free disk"
            return False, (
                f"Skipping chromium install — only {free // (1024 * 1024)}MB free disk "
                f"(need at least {MIN_FREE_DISK_BYTES // (1024 * 1024)}MB headroom on this "
                "14GB sandbox). Free up disk space first (e.g. clear old screenshots/logs) "
                "before trying again."
            )

        logger.info("Chromium missing — attempting self-install (chromium only, no --with-deps)")
        try:
            # No --with-deps: that installs apt system packages and needs
            # root, which a Pterodactyl container doesn't have. Chromium's
            # headless_shell variant runs fine on Debian slim images without
            # the extra system libs --with-deps would pull in.
            proc = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                text=True,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            _last_result = "install timed out"
            return False, (
                f"Chromium install timed out after {INSTALL_TIMEOUT_SECONDS}s — likely a slow "
                "or throttled connection on this host. Not retrying immediately."
            )
        except Exception as exc:  # noqa: BLE001
            _last_result = str(exc)
            return False, f"Chromium install failed to start: {exc}"

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            _last_result = tail or f"exit code {proc.returncode}"
            return False, f"Chromium install failed (exit {proc.returncode}): {tail}"

        if _chromium_installed():
            _last_result = ""
            logger.info("Chromium installed successfully")
            return True, "chromium installed"

        _last_result = "install reported success but binary still not found"
        return False, (
            "Playwright reported a successful install but the chromium binary still "
            "isn't found — this usually means the download landed outside the cache "
            "path Playwright expects. Screenshots will stay disabled until this is "
            "looked at directly."
        )
