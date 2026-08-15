"""Local preview service (TOOLS/Preview).

Discovers the right dev/build command for a project, starts it as a managed
background process, finds the port it listens on, and reports a health check.
Used by the Web workflow: build -> start -> find port -> test -> verify.
"""

from __future__ import annotations

import os
import re
import socket
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Preview:
    cwd: Path
    cmd: list[str]
    port: int = 0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log: list[str] = field(default_factory=list)

    def start(self) -> dict[str, Any]:
        self.log = []
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            return {"error": f"Command not found: {self.cmd[0]}"}
        return {"started": " ".join(self.cmd), "pid": self.proc.pid}

    def poll_log(self, seconds: float = 4.0) -> str:
        if self.proc is None:
            return ""
        import select

        end = time.time() + seconds
        while time.time() < end:
            if self.proc.stdout is None or self.proc.poll() is not None:
                break
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                break
            self.log.append(line.rstrip())
            if len(self.log) > 200:
                self.log = self.log[-200:]
            yield line.rstrip()

    def detect_port(self) -> int:
        for line in self.log:
            m = re.search(r"localhost[:/](\d+)", line)
            if m:
                self.port = int(m.group(1))
                return self.port
            m = re.search(r"port (\d+)", line)
            if m:
                self.port = int(m.group(1))
                return self.port
        return self.port

    def health(self, url: str | None = None) -> dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "running": False, "error": "process not running"}
        return {"ok": True, "running": True, "pid": self.proc.pid, "port": self.port}

    def stop(self) -> dict[str, Any]:
        if self.proc is None:
            return {"error": "not running"}
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.proc.terminate()
        self.proc = None
        return {"stopped": True}


def detect_command(cwd: Path) -> list[str]:
    """Pick a reasonable dev/preview command based on the project type."""
    if (cwd / "package.json").exists():
        return ["npm", "run", "dev"]
    if (cwd / "pyproject.toml").exists():
        return ["uv", "run", "uvicorn", "app.main:app"]
    if (cwd / "manage.py").exists():
        return ["python", "manage.py", "runserver", "0.0.0.0:8000"]
    if (cwd / "main.py").exists():
        return ["python", "main.py"]
    return ["python3", "-m", "http.server", str(_free_port())]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def available() -> bool:
    return shutil.which("npm") is not None or shutil.which("python") is not None


# Process registry so start/stop/health work across separate tool calls.
_RUNNING: dict[str, Preview] = {}


def start(cwd: Path) -> dict[str, Any]:
    key = str(cwd)
    if key in _RUNNING and _RUNNING[key].proc and _RUNNING[key].proc.poll() is None:
        return {"running": True, "pid": _RUNNING[key].proc.pid, "port": _RUNNING[key].port}
    p = Preview(cwd=cwd, cmd=detect_command(cwd))
    if p.cmd and p.cmd[0] == "python3" and "-m" in p.cmd:
        try:
            p.port = int(p.cmd[p.cmd.index("-m") + 2])
        except (ValueError, IndexError):
            pass
    out = p.start()
    if "error" in out:
        return out
    # give it a moment to boot and capture the port
    for _ in p.poll_log(4.0):
        pass
    out["port"] = p.detect_port()
    out["command"] = " ".join(p.cmd)
    _RUNNING[key] = p
    return out


def stop(cwd: Path) -> dict[str, Any]:
    key = str(cwd)
    p = _RUNNING.get(key)
    if p is None:
        return {"error": "No preview running for this directory."}
    res = p.stop()
    _RUNNING.pop(key, None)
    return res


def health(cwd: Path) -> dict[str, Any]:
    key = str(cwd)
    p = _RUNNING.get(key)
    if p is None:
        return {"ok": False, "running": False, "error": "no preview running"}
    return p.health()
