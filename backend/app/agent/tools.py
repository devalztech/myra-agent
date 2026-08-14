"""Myra's tool belt.

Every tool is a plain Python function registered in :data:`TOOLS` with a JSON
schema the model sees. All filesystem access goes through
``app.workspace.safe_path`` and all shell access through
``app.agent.guardrails.screen_command`` — there is no unguarded path.

Groups:
  * filesystem — list / read / write / edit / delete / move / search
  * terminal   — run shell commands inside the workspace (sandboxed cwd)
  * testing    — run a project's test command and report failures
  * archives   — zip / unzip
  * network    — http_fetch, web_search
  * browser    — open a page, read text, screenshot (Playwright when present)
  * media      — read an image (metadata + OCR-free description hook)
  * memory     — remember / recall / forget user preferences and conventions
                 (forget is a soft-delete; see app.agent.memory.MemoryStore)
  * skills     — look up a built-in language/framework skill sheet
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from ..workspace import UnsafePath, ensure_parent, relative, safe_path, workspace_root
from .guardrails import CommandBlocked, screen_command, truncate
from .memory import TRASH_TTL_DAYS

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
    ".myra",
}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    label: str = ""
    mutates: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


TOOLS: dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    *,
    label: str = "",
    mutates: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            handler=fn,
            label=label or name,
            mutates=mutates,
        )
        return fn

    return wrap


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------


@tool(
    "list_files",
    "List files and directories inside the workspace. Use this first to learn a project's layout.",
    _obj(
        {
            "path": _str("Directory relative to the workspace root. Defaults to the root."),
            "depth": {"type": "integer", "description": "How deep to walk (1-4). Default 2."},
        },
        [],
    ),
    label="Listing files",
)
def list_files(path: str = ".", depth: int = 2) -> str:
    root = safe_path(path, must_exist=True)
    if root.is_file():
        return f"{relative(root)} (file, {root.stat().st_size} bytes)"
    depth = max(1, min(int(depth or 2), 4))
    lines: list[str] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        level = len(current.parts) - base_depth
        if level >= depth:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            file = current / name
            try:
                size = file.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{relative(file)}  ({size} B)")
        if len(lines) > 800:
            lines.append("… [listing truncated]")
            break
    return "\n".join(lines) or "(empty directory)"


@tool(
    "read_file",
    "Read a UTF-8 text file from the workspace. Optionally read a line range.",
    _obj(
        {
            "path": _str("File path relative to the workspace root."),
            "start_line": {"type": "integer", "description": "1-indexed first line."},
            "end_line": {"type": "integer", "description": "1-indexed last line."},
        },
        ["path"],
    ),
    label="Reading file",
)
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    file = safe_path(path, must_exist=True)
    if file.is_dir():
        raise IsADirectoryError(f"{relative(file)} is a directory — use list_files.")
    if file.stat().st_size > settings.max_file_bytes:
        raise ValueError(
            f"{relative(file)} is larger than the {settings.max_file_bytes} byte read limit."
        )
    text = file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or len(lines)))
    numbered = [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
    return truncate("\n".join(numbered))


@tool(
    "write_file",
    "Create or overwrite a text file with the full new contents.",
    _obj({"path": _str("File path."), "content": _str("Complete file contents.")}, ["path", "content"]),
    label="Writing file",
    mutates=True,
)
def write_file(path: str, content: str) -> str:
    file = safe_path(path)
    ensure_parent(file)
    existed = file.exists()
    file.write_text(content or "", encoding="utf-8")
    verb = "Updated" if existed else "Created"
    return f"{verb} {relative(file)} ({len(content or '')} chars)"


@tool(
    "edit_file",
    "Replace an exact snippet inside a file. `old` must appear exactly once.",
    _obj(
        {
            "path": _str("File path."),
            "old": _str("Exact existing text to replace."),
            "new": _str("Replacement text (empty string deletes the snippet)."),
        },
        ["path", "old", "new"],
    ),
    label="Editing file",
    mutates=True,
)
def edit_file(path: str, old: str, new: str = "") -> str:
    file = safe_path(path, must_exist=True)
    text = file.read_text(encoding="utf-8")
    hits = text.count(old)
    if hits == 0:
        raise ValueError(f"Snippet not found in {relative(file)}.")
    if hits > 1:
        raise ValueError(f"Snippet appears {hits} times in {relative(file)} — make it unique.")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"Edited {relative(file)}"


@tool(
    "delete_path",
    "Delete a file or an entire directory inside the workspace.",
    _obj({"path": _str("File or directory path.")}, ["path"]),
    label="Deleting path",
    mutates=True,
)
def delete_path(path: str) -> str:
    target = safe_path(path, must_exist=True)
    if target == workspace_root():
        raise UnsafePath("Refusing to delete the workspace root.")
    if target.is_dir():
        shutil.rmtree(target)
        return f"Deleted directory {relative(target)}"
    target.unlink()
    return f"Deleted {relative(target)}"


@tool(
    "move_path",
    "Move or rename a file/directory inside the workspace.",
    _obj({"source": _str("Existing path."), "destination": _str("New path.")}, ["source", "destination"]),
    label="Moving path",
    mutates=True,
)
def move_path(source: str, destination: str) -> str:
    src = safe_path(source, must_exist=True)
    dst = safe_path(destination)
    ensure_parent(dst)
    shutil.move(str(src), str(dst))
    return f"Moved {relative(src)} -> {relative(dst)}"


@tool(
    "search_code",
    "Regex search across workspace files. Returns matching file:line snippets.",
    _obj(
        {
            "pattern": _str("Regular expression."),
            "path": _str("Directory to search. Defaults to the workspace root."),
            "glob": _str("Optional filename filter, e.g. '*.py'."),
        },
        ["pattern"],
    ),
    label="Searching code",
)
def search_code(pattern: str, path: str = ".", glob: str | None = None) -> str:
    root = safe_path(path, must_exist=True)
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if glob and not Path(name).match(glob):
                continue
            file = Path(dirpath) / name
            try:
                if file.stat().st_size > settings.max_file_bytes:
                    continue
                for number, line in enumerate(
                    file.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
                ):
                    if regex.search(line):
                        results.append(f"{relative(file)}:{number}: {line.strip()[:240]}")
                        if len(results) >= 200:
                            return "\n".join(results) + "\n… [more matches omitted]"
            except OSError:
                continue
    return "\n".join(results) or "No matches."


# --------------------------------------------------------------------------
# terminal / tests
# --------------------------------------------------------------------------


def _run(command: str, cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    env = {
        **os.environ,
        "HOME": str(workspace_root()),
        "MYRA_SANDBOX": "1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return {
            "command": command,
            "exitCode": proc.returncode,
            "output": truncate(output.strip()),
            "durationMs": int((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exitCode": 124,
            "output": f"Command timed out after {timeout}s.",
            "durationMs": timeout * 1000,
        }


@tool(
    "run_command",
    "Run a shell command inside the workspace sandbox. Returns exit code and output.",
    _obj(
        {
            "command": _str("Shell command to run."),
            "cwd": _str("Working directory relative to the workspace root."),
            "timeout": {"type": "integer", "description": "Seconds before the command is killed."},
        },
        ["command"],
    ),
    label="Running command",
    mutates=True,
)
def run_command(command: str, cwd: str = ".", timeout: int | None = None) -> dict[str, Any]:
    screen_command(command)
    directory = safe_path(cwd, must_exist=True)
    if directory.is_file():
        directory = directory.parent
    limit = min(int(timeout or settings.tool_timeout_seconds), settings.tool_timeout_seconds)
    return _run(command, directory, limit)


@tool(
    "run_tests",
    "Run a project's tests (auto-detects pytest / npm test / bun test when no command is given).",
    _obj(
        {
            "command": _str("Explicit test command. Optional."),
            "cwd": _str("Project directory relative to the workspace root."),
        },
        [],
    ),
    label="Running tests",
    mutates=True,
)
def run_tests(command: str | None = None, cwd: str = ".") -> dict[str, Any]:
    directory = safe_path(cwd, must_exist=True)
    if not command:
        if (directory / "package.json").exists():
            command = "bun test" if shutil.which("bun") else "npm test --silent"
        elif any(directory.glob("test_*.py")) or (directory / "tests").exists():
            command = "python3 -m pytest -q"
        else:
            command = "echo 'No test suite detected.'"
    screen_command(command)
    result = _run(command, directory, settings.tool_timeout_seconds)
    result["passed"] = result["exitCode"] == 0
    return result


# --------------------------------------------------------------------------
# archives / uploads
# --------------------------------------------------------------------------


@tool(
    "zip_paths",
    "Create a .zip archive from files/directories in the workspace.",
    _obj(
        {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to include."},
            "archive": _str("Destination .zip path."),
        },
        ["paths", "archive"],
    ),
    label="Creating archive",
    mutates=True,
)
def zip_paths(paths: list[str], archive: str) -> str:
    target = safe_path(archive)
    ensure_parent(target)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for raw in paths or []:
            item = safe_path(raw, must_exist=True)
            if item.is_dir():
                for dirpath, dirnames, filenames in os.walk(item):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in filenames:
                        file = Path(dirpath) / name
                        zf.write(file, relative(file))
                        count += 1
            else:
                zf.write(item, relative(item))
                count += 1
    return f"Wrote {relative(target)} with {count} entries."


@tool(
    "unzip_archive",
    "Extract a .zip archive inside the workspace.",
    _obj({"archive": _str(".zip path."), "destination": _str("Directory to extract into.")}, ["archive"]),
    label="Extracting archive",
    mutates=True,
)
def unzip_archive(archive: str, destination: str = ".") -> str:
    src = safe_path(archive, must_exist=True)
    out = safe_path(destination)
    out.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(src) as zf:
        for member in zf.namelist():
            # Zip-slip protection: every member is re-checked against the jail.
            resolved = safe_path(str(Path(out) / member))
            if member.endswith("/"):
                resolved.mkdir(parents=True, exist_ok=True)
                continue
            ensure_parent(resolved)
            with zf.open(member) as source, open(resolved, "wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted += 1
    return f"Extracted {extracted} files into {relative(out)}"


@tool(
    "read_image",
    "Inspect an image file in the workspace (format, dimensions, base64 preview for vision models).",
    _obj({"path": _str("Image path.")}, ["path"]),
    label="Reading image",
)
def read_image(path: str) -> dict[str, Any]:
    file = safe_path(path, must_exist=True)
    data = file.read_bytes()
    info: dict[str, Any] = {
        "path": relative(file),
        "bytes": len(data),
        "mime": mimetypes.guess_type(file.name)[0] or "application/octet-stream",
    }
    try:  # Pillow is optional
        from PIL import Image  # type: ignore

        with Image.open(file) as img:
            info["width"], info["height"] = img.size
            info["format"] = img.format
    except Exception:  # pragma: no cover - optional dependency
        info["note"] = "Install Pillow for dimensions."
    if len(data) < 1_500_000:
        info["base64"] = base64.b64encode(data).decode("ascii")[:4000]
    return info


# --------------------------------------------------------------------------
# network / browser
# --------------------------------------------------------------------------


def _require_network() -> None:
    if not settings.enable_network_tools:
        raise CommandBlocked("Network tools are disabled by configuration.")


@tool(
    "http_fetch",
    "Fetch a URL and return the response body as text (HTML is stripped to readable text).",
    _obj(
        {
            "url": _str("Absolute http(s) URL."),
            "method": _str("HTTP method. Default GET."),
            "body": _str("Optional request body."),
        },
        ["url"],
    ),
    label="Fetching URL",
)
def http_fetch(url: str, method: str = "GET", body: str | None = None) -> dict[str, Any]:
    _require_network()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed.")
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise CommandBlocked("Refusing to fetch loopback addresses.")
    request = urllib.request.Request(
        url,
        method=method.upper(),
        data=(body or "").encode() or None,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MyraAgent/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        raw = resp.read(2_000_000)
        content_type = resp.headers.get("Content-Type", "")
        status = resp.status
    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type:
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s{2,}", " ", text)
    return {"url": url, "status": status, "contentType": content_type, "text": truncate(text.strip())}


@tool(
    "web_search",
    "Search the web and return result titles, URLs and snippets.",
    _obj({"query": _str("Search query."), "limit": {"type": "integer", "description": "Max results."}}, ["query"]),
    label="Searching the web",
)
def web_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    _require_network()
    endpoint = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": "Mozilla/5.0 (compatible; MyraAgent/1.0)"}
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        html = resp.read(1_500_000).decode("utf-8", errors="replace")
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>[\s\S]*?)</a>', html
    ):
        href = urllib.parse.unquote(match.group("href"))
        if "uddg=" in href:
            href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        title = re.sub(r"<[^>]+>", "", match.group("title")).strip()
        results.append({"title": title, "url": href})
        if len(results) >= max(1, min(int(limit or 5), 10)):
            break
    return results


@tool(
    "browse_page",
    "Open a page in Myra's internal browser, return its readable text, and optionally save a screenshot.",
    _obj(
        {
            "url": _str("Absolute http(s) URL."),
            "screenshot": {"type": "boolean", "description": "Save a PNG screenshot in the workspace."},
        },
        ["url"],
    ),
    label="Browsing page",
)
def browse_page(url: str, screenshot: bool = False) -> dict[str, Any]:
    _require_network()
    if not settings.enable_browser_tools:
        raise CommandBlocked("Browser tools are disabled by configuration.")
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        result = http_fetch(url)
        result["engine"] = "http"
        result["note"] = (
            "Playwright is not installed — returned static HTML text instead of a rendered page. "
            "Install it with `pip install playwright && playwright install chromium`."
        )
        return result

    shots_dir = workspace_root() / ".myra" / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"url": url, "engine": "chromium"}
    with sync_playwright() as pw:  # pragma: no cover - needs a browser
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        out["title"] = page.title()
        out["text"] = truncate(page.inner_text("body"))
        if screenshot:
            shot = shots_dir / f"page-{int(time.time())}.png"
            page.screenshot(path=str(shot))
            out["screenshot"] = relative(shot)
        browser.close()
    return out


@tool(
    "screenshot_page",
    "Take a screenshot of a URL and save it in the workspace.",
    _obj({"url": _str("Absolute http(s) URL.")}, ["url"]),
    label="Taking screenshot",
)
def screenshot_page(url: str) -> dict[str, Any]:
    return browse_page(url, screenshot=True)


# --------------------------------------------------------------------------
# memory / skills (bound at runtime by the agent loop)
# --------------------------------------------------------------------------


@tool(
    "remember",
    "Save a durable user preference or project convention so future runs know it.",
    _obj(
        {
            "key": _str("Short stable key, e.g. 'style.indent'."),
            "value": _str("The rule to remember."),
            "kind": _str("preference | convention | fact"),
        },
        ["key", "value"],
    ),
    label="Saving memory",
    mutates=True,
)
def remember(key: str, value: str, kind: str = "preference", _memory=None) -> str:
    if _memory is None:
        raise RuntimeError("Memory store unavailable.")
    _memory.remember(key=key, value=value, kind=kind)
    return f"Remembered {kind}: {key}"


@tool(
    "recall",
    "Search Myra's long-term memory for preferences and conventions.",
    _obj({"query": _str("Optional search text.")}, []),
    label="Recalling memory",
)
def recall(query: str = "", _memory=None) -> list[dict[str, str]]:
    if _memory is None:
        return []
    return _memory.search(query)


@tool(
    "forget",
    "Forget a remembered preference or convention by its key. This is "
    "recoverable — the memory goes to trash and can be restored later — "
    "it is not a permanent delete.",
    _obj({"key": _str("The exact key used when it was remembered.")}, ["key"]),
    label="Forgetting memory",
    mutates=True,
)
def forget(key: str, _memory=None) -> str:
    if _memory is None:
        raise RuntimeError("Memory store unavailable.")
    match = next((m for m in _memory.search(key) if m["key"] == key), None)
    if match is None:
        return f"No memory found for key: {key}"
    _memory.forget(match["id"])
    return f"Forgot {key} (recoverable from trash for {TRASH_TTL_DAYS} days)."


@tool(
    "get_skill",
    "Load Myra's built-in skill sheet for a language or framework (javascript, typescript, react, node, express, postgresql, sqlite, python, cpp, html, css).",
    _obj({"name": _str("Skill name.")}, ["name"]),
    label="Loading skill",
)
def get_skill(name: str) -> str:
    from .skills import skill_text

    return skill_text(name)


def tool_schemas(exclude: set[str] | None = None) -> list[dict[str, Any]]:
    return [t.schema() for name, t in TOOLS.items() if not exclude or name not in exclude]


def describe_tools() -> str:
    return "\n".join(
        f"- {t.name}({', '.join(t.parameters.get('properties', {}).keys())}): {t.description}"
        for t in TOOLS.values()
    )


def call_tool(name: str, arguments: dict[str, Any], *, memory: Any = None) -> Any:
    tool_def = TOOLS.get(name)
    if tool_def is None:
        raise KeyError(f"Unknown tool: {name}")
    kwargs = dict(arguments or {})
    if name in {"remember", "recall", "forget"}:
        kwargs["_memory"] = memory
    return tool_def.handler(**kwargs)


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
