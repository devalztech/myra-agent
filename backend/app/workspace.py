"""Workspace isolation — Myra's own sandboxed working directory.

Everything the agent can touch lives under a single root directory that is
deliberately OUTSIDE the Pterodactyl server files (``/home/container`` by
default). Every filesystem and terminal tool resolves user-supplied paths
through :func:`safe_path`, which:

  * resolves symlinks and ``..`` traversal *before* checking containment,
  * rejects anything that escapes the workspace root,
  * rejects anything inside an explicitly protected path (panel files, /etc,
    ssh keys, /proc, ...), even if the workspace root were mis-configured.

This is the single choke point for path safety; tools never call
``Path.open`` on raw user input.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings


class UnsafePath(PermissionError):
    """Raised when a path escapes the workspace or hits a protected path."""


def workspace_root() -> Path:
    root = Path(settings.workspace_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_roots() -> list[Path]:
    """Protected paths, with Myra's own workspace carved back out.

    The default workspace (``/home/container/myra``) is deliberately a
    *subfolder inside* one of the default protected paths
    (``/home/container``, the Pterodactyl panel root) — see config.py. A
    plain containment check would then treat Myra's own directory as
    protected too, blocking every install/build/delete tool from working
    at all inside the one place they're supposed to. So: a protected root
    is skipped here if the workspace root sits inside it (the workspace is
    carved out of that root, everything else in it stays protected), and
    skipped outright if it exactly equals the workspace root.
    """
    root = workspace_root()
    roots: list[Path] = []
    for raw in settings.protected_paths:
        try:
            protected = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if protected == root or _is_within(root, protected):
            continue
        roots.append(protected)
    return roots


def safe_path(raw: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    """Resolve ``raw`` inside the workspace or raise :class:`UnsafePath`."""
    root = workspace_root()
    text = str(raw or "").strip()
    if not text or text in {".", "./"}:
        return root
    if "\x00" in text:
        raise UnsafePath("Path contains a null byte.")

    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()

    if not _is_within(resolved, root) and resolved != root:
        raise UnsafePath(
            f"Path escapes Myra's workspace: {text}. Myra can only work inside {root}."
        )

    for protected in _protected_roots():
        if resolved == protected or _is_within(resolved, protected):
            raise UnsafePath(f"Path is protected and cannot be accessed: {protected}")

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"No such file or directory: {relative(resolved)}")
    return resolved


def relative(path: Path) -> str:
    """Workspace-relative display path (never leaks absolute host paths)."""
    root = workspace_root()
    try:
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return str(path)
    return str(rel) if str(rel) != "." else "."


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def workspace_info() -> dict[str, object]:
    root = workspace_root()
    total = 0
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "__pycache__"}]
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
                files += 1
            except OSError:
                continue
    return {
        "root": str(root),
        "files": files,
        "sizeBytes": total,
        "protectedPaths": settings.protected_paths,
    }
