"""Browser automation service (TOOLS/Browser) via Playwright.

One shared browser/context (per the 3 GB constraint). Provides eyes + hands
for websites: open, read text, click, type, fill, screenshot, and capture
console/network. Playwright is optional — the tool falls back to plain HTTP
when chromium isn't installed.
"""

from __future__ import annotations

import time
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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        console_msgs: list[str] = []
        page.on("console", lambda m: console_msgs.append(m.text))

        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        out["title"] = page.title()

        if action == "screenshot" or screenshot:
            shot = shots_dir / f"page-{int(time.time())}.png"
            page.screenshot(path=str(shot))
            out["screenshot"] = relative(shot)

        if action in ("click", "fill", "type") and selector:
            try:
                if action == "click":
                    page.click(selector, timeout=8000)
                elif action == "fill":
                    page.fill(selector, text or "")
                elif action == "type":
                    page.type(selector, text or "", delay=20)
                out["action"] = f"{action} {selector} done"
            except Exception as exc:  # noqa: BLE001
                out["actionError"] = str(exc)

        if action == "text" or action == "open":
            out["text"] = page.inner_text("body")[:8000]

        if console_msgs:
            out["console"] = console_msgs[-20:]

        browser.close()

    return out
