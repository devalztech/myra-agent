"""Browser automation service (TOOLS/Browser) via Playwright.

One real browser session per chat session, kept alive across calls instead
of a fresh throwaway chromium per action. This is what makes multi-step
flows possible — "open the login page, fill the form, click sign in, read
the inbox" needs the SECOND call to still see the cookies/DOM state the
FIRST call produced. A launch-and-close-per-call design (the old shape of
this file) makes that structurally impossible: every action was its own
private incognito browser.

Sessions are keyed by an opaque `session_id` the agent loop passes in (its
chat session id) and reaped after IDLE_TIMEOUT of inactivity by a background
sweeper, so a forgotten tab doesn't hold a chromium process open forever.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..workspace import relative, workspace_root

logger = logging.getLogger("myra.browser")

IDLE_TIMEOUT = 10 * 60  # close a session's browser after 10 min of no calls
NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
MAX_TEXT_CHARS = 12_000  # matches agent.guardrails.truncate's default cap

# External screenshot service used as a fallback when chromium can't be
# installed/run on a small sandbox (no root, low disk). Lets myra still hand
# the user a real rendered screenshot of any public URL even without a local
# browser binary.
SCREENSHOT_API = "https://eliteprotech-apis.zone.id/ssweb"


def screenshot_via_api(url: str, target: Path | None = None) -> dict[str, Any]:
    """Screenshot a URL through the external API, saving to workspace if target given."""
    api_url = f"{SCREENSHOT_API}?url={urllib.parse.quote(url)}"
    req = urllib.request.Request(
        api_url,
        headers={
            "accept": "*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if target is None:
        target = workspace_root() / ".myra" / "screenshots" / f"ss-{int(time.time())}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"screenshot": relative(target), "engine": "api", "bytes": len(data)}


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class _Session:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    last_used: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)


class BrowserManager:
    """Owns one Playwright browser context per chat session.

    All Playwright calls happen on whatever thread calls into this class —
    callers are responsible for running off the asyncio event-loop thread
    (sync_playwright refuses to run on one), same as before.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._sweeper_started = False

    def _start_sweeper(self) -> None:
        if self._sweeper_started:
            return
        self._sweeper_started = True

        def _loop() -> None:
            while True:
                time.sleep(60)
                now = time.monotonic()
                stale: list[str] = []
                with self._lock:
                    for sid, sess in self._sessions.items():
                        if now - sess.last_used > IDLE_TIMEOUT:
                            stale.append(sid)
                    for sid in stale:
                        self._close_locked(sid)

        threading.Thread(target=_loop, daemon=True, name="myra-browser-sweeper").start()

    def _close_locked(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            return
        try:
            sess.context.close()
        except Exception:
            pass
        try:
            sess.browser.close()
        except Exception:
            pass
        try:
            sess.playwright.stop()
        except Exception:
            pass
        logger.info("Closed idle browser session %s", session_id)

    def _get_or_create(self, session_id: str) -> _Session:
        with self._lock:
            self._start_sweeper()
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess.last_used = time.monotonic()
                return sess

            from playwright.sync_api import sync_playwright  # noqa: PLC0415

            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                ],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            page = context.new_page()
            sess = _Session(playwright=pw, browser=browser, context=context, page=page)
            self._sessions[session_id] = sess
            logger.info("Opened browser session %s", session_id)
            return sess

    def close(self, session_id: str) -> None:
        with self._lock:
            self._close_locked(session_id)

    def act(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        """Run one browser action against this session's persistent page."""
        sess = self._get_or_create(session_id)
        with sess.lock:
            return _do_action(sess.page, **kwargs)


_manager = BrowserManager()


def _capture_console(page: Any) -> list[str]:
    msgs: list[str] = []
    page.on("console", lambda m: msgs.append(f"[{m.type}] {m.text}"))
    return msgs


def _do_action(
    page: Any,
    *,
    action: str,
    url: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    screenshot: bool = False,
) -> dict[str, Any]:
    shots_dir = workspace_root() / ".myra" / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"engine": "chromium"}
    console_msgs: list[str] = []

    try:
        if action == "open":
            if not url:
                return {"error": "action 'open' requires a url."}
            console_msgs = _capture_console(page)
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # Give SPA content a beat to render past first paint, but don't
            # hang the whole run if the page never truly goes idle (trackers,
            # websockets, polling widgets keep plenty of "real" pages busy
            # forever).
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            out["title"] = page.title()
            out["text"] = page.inner_text("body")[:MAX_TEXT_CHARS]

        elif action == "text":
            out["title"] = page.title()
            out["text"] = page.inner_text("body")[:MAX_TEXT_CHARS]

        elif action == "click":
            if not selector:
                return {"error": "action 'click' requires a selector."}
            page.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
            page.click(selector, timeout=ACTION_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            out["action"] = f"clicked {selector}"
            out["title"] = page.title()

        elif action == "fill":
            if not selector:
                return {"error": "action 'fill' requires a selector."}
            page.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
            page.fill(selector, text or "", timeout=ACTION_TIMEOUT_MS)
            out["action"] = f"filled {selector}"

        elif action == "type":
            if not selector:
                return {"error": "action 'type' requires a selector."}
            page.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
            page.type(selector, text or "", delay=20)
            out["action"] = f"typed into {selector}"

        elif action == "press":
            if not selector:
                return {"error": "action 'press' requires a selector."}
            page.wait_for_selector(selector, state="visible", timeout=ACTION_TIMEOUT_MS)
            page.press(selector, text or "Enter", timeout=ACTION_TIMEOUT_MS)
            out["action"] = f"pressed {text or 'Enter'} on {selector}"

        elif action == "scroll":
            if selector:
                page.locator(selector).scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
                out["action"] = f"scrolled to {selector}"
            else:
                page.mouse.wheel(0, 1600)
                out["action"] = "scrolled down"

        elif action == "wait_for":
            if not selector:
                return {"error": "action 'wait_for' requires a selector."}
            page.wait_for_selector(selector, state="visible", timeout=NAV_TIMEOUT_MS)
            out["action"] = f"'{selector}' is visible"

        elif action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            out["title"] = page.title()
            out["url"] = page.url

        elif action == "screenshot":
            shot = shots_dir / f"page-{int(time.time())}.png"
            page.screenshot(path=str(shot))
            out["screenshot"] = relative(shot)

        else:
            return {"error": f"Unknown browser action: {action}"}

    except Exception as exc:  # noqa: BLE001
        out["actionError"] = f"{type(exc).__name__}: {exc}"

    out["url"] = page.url
    if screenshot and action != "screenshot":
        shot = shots_dir / f"page-{int(time.time())}.png"
        try:
            page.screenshot(path=str(shot))
            out["screenshot"] = relative(shot)
        except Exception:
            pass
    if console_msgs:
        out["console"] = console_msgs[-20:]

    return out


def browser_action(
    session_id: str,
    action: str,
    *,
    url: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    screenshot: bool = False,
) -> dict[str, Any]:
    """Public entry point used by the `browser` tool.

    Keeps one real page alive per session_id across calls, so a login ->
    click -> read flow actually shares state instead of each step getting
    its own private incognito browser.
    """
    if not _browser_available():
        return _chromium_fallback(action, url, screenshot=screenshot)

    from .browser_setup import ensure_chromium

    ok, message = ensure_chromium()
    if not ok:
        return _chromium_fallback(action, url, reason=message, screenshot=screenshot)

    try:
        return _manager.act(
            session_id,
            action=action,
            url=url,
            selector=selector,
            text=text,
            screenshot=screenshot,
        )
    except Exception as exc:  # noqa: BLE001
        # Browser binary missing, session crashed, etc — never crash the
        # agent run over it. Drop the (possibly wedged) session so the next
        # call gets a clean one instead of retrying a broken page forever.
        _manager.close(session_id)
        return {
            "error": f"Browser action failed: {type(exc).__name__}: {exc}",
            "note": "The browser session was reset — try again.",
        }


def close_session(session_id: str) -> None:
    _manager.close(session_id)


def _chromium_fallback(
    action: str, url: str | None, reason: str = "", screenshot: bool = False
) -> dict[str, Any]:
    """Graceful degradation when chromium can't run on this sandbox.

    The box is small (no root for `--with-deps`, often low free disk), so
    chromium genuinely can't always be installed. Rather than fail the whole
    run, fall back to an external screenshot API when a screenshot was asked
    for, else fetch the page's text over plain HTTP for the `open` action.
    Other actions still fail cleanly with a reason.
    """
    if screenshot and url:
        # User explicitly wants a rendered image — use the external service
        # even without a local browser.
        try:
            return screenshot_via_api(url)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"External screenshot failed: {exc}", "engine": "api"}
    if action == "open" and url:
        try:
            from ..agent.tools import http_fetch

            result = http_fetch(url)
            result["engine"] = "http"
            result["note"] = (
                "Chromium is not available on this box (no root / low disk), so this "
                "page was fetched as plain HTML text instead of a rendered browser. "
                "You can read/analyze it but cannot click, type, or screenshot."
            )
            if reason:
                result["chromiumReason"] = reason
            return result
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Page fetch failed: {exc}"}
    return {
        "error": (
            "Browser unavailable: "
            + (reason or "Playwright/chromium is not installed and can't be on this sandbox")
            + ". Action requires a real browser and was not run."
        )
    }
