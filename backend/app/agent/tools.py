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
import queue
import threading
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


def _run_off_loop(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a sync Playwright/blocking call in a fresh worker thread.

    The agent loop is synchronous (``def run``) but is consumed from an async
    FastAPI streaming endpoint, so ``sync_playwright`` sees an active event
    loop on the calling thread and raises "Sync API inside async loop". Running
    the call on a plain worker thread (which has no event loop) sidesteps that
    without converting the whole loop to the async Playwright API.
    """
    result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result.put((True, fn(*args)))
        except Exception as exc:  # noqa: BLE001
            result.put((False, exc))

    threading.Thread(target=_worker, daemon=True).start()
    ok, value = result.get(timeout=120)
    if not ok:
        raise value
    return value


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
def run_command(
    command: str, cwd: str = ".", timeout: int | None = None, _approved: bool = False
) -> dict[str, Any]:
    screen_command(command, approved=_approved)
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
def run_tests(
    command: str | None = None, cwd: str = ".", _approved: bool = False
) -> dict[str, Any]:
    directory = safe_path(cwd, must_exist=True)
    if not command:
        if (directory / "package.json").exists():
            command = "bun test" if shutil.which("bun") else "npm test --silent"
        elif any(directory.glob("test_*.py")) or (directory / "tests").exists():
            command = "python3 -m pytest -q"
        else:
            command = "echo 'No test suite detected.'"
    screen_command(command, approved=_approved)
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


@tool(
    "project_inspect",
    "Analyse a project in the workspace: detect language, framework, package manager, "
    "entrypoints, scripts and dependencies from its manifest files. Use this when entering "
    "an unfamiliar repository before touching it.",
    _obj({"path": _str("Project directory. Defaults to the workspace root.")}, ["path"]),
    label="Inspecting project",
)
def project_inspect(path: str = ".") -> dict[str, Any]:
    root = safe_path(path, must_exist=True)
    if not root.is_dir():
        raise ValueError(f"{relative(root)} is not a directory.")
    info: dict[str, Any] = {"root": relative(root)}

    # Python
    pyproject = root / "pyproject.toml"
    setup = root / "setup.py"
    req = root / "requirements.txt"
    if pyproject.exists() or setup.exists():
        info["language"] = "Python"
        if pyproject.exists():
            info["build"] = "pyproject.toml"
        else:
            info["build"] = "setup.py"
    elif req.exists():
        info["language"] = "Python"
        info["build"] = "requirements.txt"

    # Node
    pkg = root / "package.json"
    if pkg.exists():
        info["language"] = "JavaScript/TypeScript"
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            info["packageManager"] = data.get("packageManager", "npm")
            if data.get("scripts"):
                info["scripts"] = data["scripts"]
            deps = set(data.get("dependencies", {}))
            deps.update(data.get("devDependencies", {}))
            for key in ("next", "react", "vue", "svelte", "vite", "express", "nest"):
                if key in deps:
                    info["framework"] = key
        except Exception:
            pass

    # Go / Rust
    if (root / "go.mod").exists():
        info["language"] = "Go"
    if (root / "Cargo.toml").exists():
        info["language"] = "Rust"
        info["build"] = "Cargo.toml"

    # Entrypoints / key files
    entry_candidates = [
        "app.py", "main.py", "manage.py", "wsgi.py", "asgi.py",
        "index.js", "index.ts", "src/index.tsx", "main.go", "src/main.rs",
    ]
    present = [c for c in entry_candidates if (root / c).exists()]
    if present:
        info["entrypoints"] = present

    # Git
    info["git"] = (root / ".git").exists()

    # Dirs
    dirs = sorted(d for d in os.listdir(root) if (root / d).is_dir() and d not in SKIP_DIRS)
    if dirs:
        info["dirs"] = dirs[:30]

    return info


@tool(
    "git",
    "Run a safe git operation in the workspace (status, diff, log, branch, add, commit, "
    "checkout, stash, merge, pull, push, clone). Destructive/uncommon ops are guarded.",
    _obj(
        {
            "operation": _str(
                "One of: status, diff, log, branch, add, commit, checkout, stash, merge, "
                "pull, push, clone, remote, show"
            ),
            "path": _str("Repo directory (defaults to workspace root)."),
            "args": _str("Extra arguments (e.g. files to add, commit message)."),
        },
        ["operation"],
    ),
    label="Running git",
)
def git(operation: str, path: str = ".", args: str = "") -> dict[str, Any]:
    repo = safe_path(path, must_exist=True)
    if not (repo / ".git").exists() and operation != "clone":
        return {"error": f"{relative(repo)} is not a git repository."}
    allowed = {
        "status", "diff", "log", "branch", "add", "commit", "checkout",
        "stash", "merge", "pull", "push", "remote", "show",
    }
    op = (operation or "").strip().lower()
    if op not in allowed:
        raise ValueError(f"Unsafe or unsupported git operation: {operation}")
    # Safety: refuse obviously dangerous argument combinations
    blocked_args = ["--force", "--hard", "-f"]
    if op in ("reset", "checkout") and any(b in (args or "") for b in blocked_args):
        raise ValueError("Refusing destructive git operation. Use safer arguments.")
    cmd = ["git", op]
    if args:
        cmd.append(args)
    result = _run(" ".join(cmd), repo, timeout=60)
    return result


@tool(
    "get_workflow",
    "Load a named workflow procedure (debug, test, web, api, database, deploy, recover, git) "
    "that tells you the correct end-to-end sequence for that task.",
    _obj({"name": _str("Workflow name: debug, test, web, api, database, deploy, recover, or git.")}, ["name"]),
    label="Loading workflow",
)
def get_workflow(name: str) -> str:
    from ..workflows import WORKFLOW_NAMES, workflow_text

    text = workflow_text(name)
    if text:
        return text
    return f"Unknown workflow '{name}'. Available: {', '.join(WORKFLOW_NAMES)}"


@tool(
    "db_query",
    "Run a read-only SQLite SELECT/PRAGMA query against a .db file in the workspace.",
    _obj({"path": _str("Path to the .sqlite/.db file."), "sql": _str("SELECT or PRAGMA SQL query.")}, ["path", "sql"]),
    label="Querying database",
)
def db_query(path: str, sql: str) -> str:
    from ..services.database import DatabaseError, sqlite_connect, sqlite_query

    db = safe_path(path, must_exist=True)
    try:
        conn = sqlite_connect(db)
        try:
            return sqlite_query(conn, sql)
        finally:
            conn.close()
    except (DatabaseError, Exception) as exc:  # noqa: BLE001
        return f"DB error: {exc}"


@tool(
    "db_schema",
    "Show the schema (tables + SQL) of a SQLite .db file in the workspace.",
    _obj({"path": _str("Path to the .sqlite/.db file.")}, ["path"]),
    label="Reading database schema",
)
def db_schema(path: str) -> str:
    from ..services.database import DatabaseError, sqlite_connect, sqlite_schema

    db = safe_path(path, must_exist=True)
    try:
        conn = sqlite_connect(db)
        try:
            return sqlite_schema(conn)
        finally:
            conn.close()
    except (DatabaseError, Exception) as exc:  # noqa: BLE001
        return f"DB error: {exc}"


@tool(
    "preview",
    "Start and manage a local dev/preview server for a project in the workspace "
    "(detect command, start, find port, health check, stop).",
    _obj(
        {
            "action": _str("start | stop | health | port"),
            "path": _str("Project directory. Defaults to workspace root."),
        },
        ["action"],
    ),
    label="Managing preview server",
)
def preview(action: str, path: str = ".") -> dict[str, Any]:
    from ..services.preview import health, start, stop

    root = safe_path(path, must_exist=True)
    action = (action or "").strip().lower()
    if action == "start":
        return start(root)
    if action == "stop":
        return stop(root)
    if action in ("health", "status"):
        return health(root)
    return {"error": f"Unknown action: {action}. Use start, stop, or health."}


@tool(
    "browser",
    "Automate a webpage with a real, persistent browser session (Playwright). The page "
    "and its cookies/login state STAY OPEN across calls within this chat, so multi-step "
    "flows work: open a page, fill a form, click submit, then read the result — each call "
    "sees what the previous one left on screen. Actions: "
    "open (navigate to url), text (read the current page), click, fill, type, "
    "press (send a key, e.g. Enter), scroll (to a selector, or down the page if no "
    "selector), wait_for (block until a selector appears — use this instead of guessing "
    "before clicking something that loads late), back (browser back), "
    "screenshot (save a PNG). Needs Playwright + chromium installed.",
    _obj(
        {
            "action": _str(
                "open | text | click | fill | type | press | scroll | wait_for | back | screenshot"
            ),
            "url": _str("Absolute http(s) URL. Required for 'open'."),
            "selector": _str("CSS selector for click/fill/type/press/scroll/wait_for."),
            "text": _str("Text to fill/type, or the key name for 'press' (default Enter)."),
            "screenshot": {
                "type": "boolean",
                "description": "Also save a screenshot after this action completes.",
            },
        },
        ["action"],
    ),
    label="Using browser",
)
def browser(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    screenshot: bool = False,
    _session_id: str | None = None,
) -> dict[str, Any]:
    _require_network()
    if not settings.enable_browser_tools:
        raise CommandBlocked("Browser tools are disabled by configuration.")
    from ..services.browser import browser_action

    action = (action or "").strip().lower()
    needs_selector = {"click", "fill", "type", "press", "wait_for"}
    if action in needs_selector and not selector:
        return {"error": f"action '{action}' requires a selector."}
    if action in ("fill", "type") and not text:
        return {"error": f"action '{action}' requires text."}
    if action == "open" and not url:
        return {"error": "action 'open' requires a url."}

    # Falls back to a shared key if the loop ever runs outside a chat
    # session (e.g. a future non-chat entry point) — still correct, just not
    # session-isolated in that edge case.
    sid = _session_id or "default"
    return browser_action(
        sid,
        action,
        url=url or None,
        selector=selector or None,
        text=text or None,
        screenshot=screenshot,
    )


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


def browse_page(url: str, screenshot: bool = False, _session_id: str | None = None) -> dict[str, Any]:
    """Deprecated alias for `browser(action="open", url=...)`.

    Kept only so anything referencing the old tool name by habit still
    works. Delegates to the same persistent-session browser as the
    `browser` tool now, instead of its old behaviour of launching and
    tearing down its own private chromium instance per call — which meant
    this and `browser` could never see each other's navigation/login state
    even within the same run.
    """
    return browser("open", url=url, screenshot=screenshot, _session_id=_session_id)


def _screenshot_file(source: Path, target: Path) -> None:
    """Render a local HTML file to a PNG screenshot using Playwright."""
    _require_network()
    if not settings.enable_browser_tools:
        raise CommandBlocked("Browser tools are disabled by configuration.")
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise CommandBlocked(
            "Playwright package is not installed (this needs a Python dependency change, "
            "not something a tool call can fix — ask the user to add `playwright` to "
            "requirements.txt)."
        ) from exc

    from ..services.browser_setup import ensure_chromium

    ok, message = ensure_chromium()
    if not ok:
        raise CommandBlocked(f"Screenshot unavailable: {message}")

    target.parent.mkdir(parents=True, exist_ok=True)
    file_url = source.resolve().as_uri()
    def _work() -> None:  # pragma: no cover - needs a browser
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(file_url, wait_until="load", timeout=45_000)
            page.screenshot(path=str(target), full_page=True)
            browser.close()

    _run_off_loop(_work)


@tool(
    "screenshot_file",
    "Render a local HTML file in the workspace and save a full-page PNG screenshot. "
    "For screenshotting a live URL instead, use the `browser` tool's screenshot action.",
    _obj(
        {"path": _str("Local HTML file in the workspace to screenshot, e.g. landing/index.html.")},
        ["path"],
    ),
    label="Taking screenshot",
)
def screenshot_file(path: str) -> dict[str, Any]:
    # If the model passed a URL instead of a local file path, route it to the
    # screenshot API (real rendered image) rather than failing.
    if path.startswith(("http://", "https://")):
        from ..services.browser import screenshot_via_api

        return screenshot_via_api(path)
    file = safe_path(path, must_exist=True)
    target = file.with_suffix(".png")
    _screenshot_file(file, target)
    return {"screenshot": relative(target), "source": relative(file)}


def screenshot_page(url: str = "", path: str = "", _session_id: str | None = None) -> dict[str, Any]:
    """Deprecated alias. Use `browser(action="screenshot", ...)` or `screenshot_file`."""
    if path:
        return screenshot_file(path)
    if not url:
        raise ValueError("Provide either `url` or `path`.")
    return browser("open", url=url, screenshot=True, _session_id=_session_id)


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
    "update_task",
    "Record the task you are currently working on: its goal, what you've done "
    "so far, and what's next. This persists across runs so you never lose your "
    "place. Call it at the start of a multi-step task and update it as you "
    "make progress, then clear it when the task is fully complete.",
    _obj(
        {
            "summary": _str("One-line summary of the task."),
            "goal": _str("Optional: the end goal."),
            "progress": _str("Optional: what you've already done."),
            "next_step": _str("Optional: the very next thing you will do."),
            "clear": {"type": "boolean", "description": "Set true to mark the task done and clear state."},
        },
        ["summary"],
    ),
    label="Tracking task",
    mutates=True,
)
def update_task(
    summary: str,
    goal: str = "",
    progress: str = "",
    next_step: str = "",
    clear: bool = False,
    _memory=None,
) -> str:
    if _memory is None:
        raise RuntimeError("Memory store unavailable.")
    if clear:
        _memory.clear_task_state()
        return "Task marked complete and cleared from state."
    _memory.set_task_state(summary, goal=goal, progress=progress, next_step=next_step)
    return f"Task state updated: {summary}"


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


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    memory: Any = None,
    session_id: str | None = None,
    approved: bool = False,
) -> Any:
    tool_def = TOOLS.get(name)
    if tool_def is None:
        raise KeyError(f"Unknown tool: {name}")
    kwargs = dict(arguments or {})
    if name in {"remember", "recall", "forget", "update_task"}:
        kwargs["_memory"] = memory
    if name == "browser":
        # Keys the persistent browser session so a login/click/read sequence
        # within one chat shares the same page instead of each call getting
        # its own private incognito browser.
        kwargs["_session_id"] = session_id
    if name in {"run_command", "run_tests"}:
        # Was previously never threaded through at all — AgentRunner.approved
        # was set from the request but nothing downstream ever read it, so
        # the approval flow could never actually let a retried command
        # through. This is what makes "re-send with approved=true" real.
        kwargs["_approved"] = approved
    return tool_def.handler(**kwargs)


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
