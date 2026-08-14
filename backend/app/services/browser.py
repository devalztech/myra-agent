"""Browser automation service (TOOLS/Browser) via Playwright.

One shared browser/context (per the 3 GB constraint). Provides eyes + hands
for websites: open, read text, click, type, fill, screenshot, and capture
console/network. Playwright is optional — the tool falls back to plain HTTP
when chromium isn't installed.
"""

from __future__ import annotations

import time
import queue
import threading
from pathlib import Path
from typing import Any

from ..config import settings
from ..workspace import relative, workspace_root


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        return True
    except Exception:
        return False


def open_page(url: str, *, action: str = "open", selector: str | None = None,
              text: str | None = None, screenshot: bool = False) -> dict[str, Any]:
    if not _browser_available():
        return {"error": "Playwright is not installed. Install it with: pip install playwright && playwright install chromium"}

    shots_dir = workspace_root() / ".myra" / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"url": url, "engine": "chromium"}

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    def _work() -> dict[str, Any]:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            console_msgs: list[str] = []
            page.on("console", lambda m: console_msgs.append(m.text))

            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            local: dict[str, Any] = {"title": page.title()}

            if action == "screenshot" or screenshot:
                shot = shots_dir / f"page-{int(time.time())}.png"
                page.screenshot(path=str(shot))
                local["screenshot"] = relative(shot)

            if action in ("click", "fill", "type") and selector:
                try:
                    if action == "click":
                        page.click(selector, timeout=8000)
                    elif action == "fill":
                        page.fill(selector, text or "")
                    elif action == "type":
                        page.type(selector, text or "", delay=20)
                    local["action"] = f"{action} {selector} done"
                except Exception as exc:  # noqa: BLE001
                    local["actionError"] = str(exc)

            if action == "text" or action == "open":
                local["text"] = page.inner_text("body")[:8000]

            if console_msgs:
                local["console"] = console_msgs[-20:]

            browser.close()
        return local

    # The agent loop runs on the event-loop thread, where sync_playwright
    # refuses to run. Execute on a plain worker thread instead.
    result_queue: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_queue.put((True, _work()))
        except Exception as exc:  # noqa: BLE001
            result_queue.put((False, exc))

    threading.Thread(target=_worker, daemon=True).start()
    ok, value = result_queue.get(timeout=120)
    if not ok:
        # Browser binary missing at launch time — don't crash the agent run.
        return {
            "url": url,
            "error": f"Chromium could not launch: {value}",
            "note": "Run `playwright install chromium` (or the setup script) to enable browsing.",
        }
    out.update(value)

    return out
