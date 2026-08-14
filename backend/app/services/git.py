"""Git / GitHub integration layer (TOOLS/Git + TOOLS/GitHub).

Wraps git operations and (optionally) GitHub API calls behind safe, focused
functions. The agent's ``git`` tool uses these; destructive operations are
guarded at the tool layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run_git(repo: Path, args: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out")
    except FileNotFoundError:
        raise GitError("git is not installed on this server")
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "").strip()[:800])
    return (proc.stdout or "").strip()


def is_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def status(repo: Path) -> str:
    return _run_git(repo, ["status", "--short", "--branch"])


def diff(repo: Path, *paths: str) -> str:
    return _run_git(repo, ["diff", "--stat"] + list(paths))


def log(repo: Path, n: int = 10) -> str:
    return _run_git(repo, ["log", "--oneline", f"-{n}"])


def branch(repo: Path) -> str:
    return _run_git(repo, ["branch", "-a"])


def current_branch(repo: Path) -> str:
    out = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out.splitlines()[0] if out else ""


def add(repo: Path, *paths: str) -> str:
    return _run_git(repo, ["add", "--"] + list(paths))


def commit(repo: Path, message: str) -> str:
    if not message:
        raise GitError("commit message is required")
    return _run_git(repo, ["commit", "-m", message])


def pull(repo: Path) -> str:
    return _run_git(repo, ["pull", "--ff-only"])


def push(repo: Path, remote: str = "origin", branch: str | None = None) -> str:
    cmd = ["push", remote]
    if branch:
        cmd.append(branch)
    return _run_git(repo, cmd)


def clone(repo: Path, url: str, destination: str | None = None) -> str:
    cmd = ["clone", url]
    if destination:
        cmd.append(destination)
    return _run_git(repo, cmd, timeout=180)
