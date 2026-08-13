"""Context engineering: give the model only what matters.

Instead of pasting the whole workspace into the prompt, Myra builds a compact
context block:

  * a shallow project map (paths only, noise directories skipped),
  * the files most relevant to the request, scored by filename/keyword overlap
    and truncated to a per-file budget,
  * the memory digest,
  * a rolling window of the conversation.

Total size is capped by ``max_context_chars`` so a 4k-context local model is
never blown out.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..workspace import relative, workspace_root
from .tools import SKIP_DIRS

CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".css", ".html",
    ".sql", ".sh", ".yml", ".yaml", ".toml", ".cpp", ".hpp", ".c", ".h", ".txt",
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower()))


@dataclass
class ProjectContext:
    tree: str
    files: list[tuple[str, str]]
    root: str

    def as_prompt(self) -> str:
        blocks = [f"Workspace root: {self.root}", "Project map:", self.tree or "(empty workspace)"]
        for path, content in self.files:
            blocks.append(f"\n--- {path} ---\n{content}")
        return "\n".join(blocks)


def project_map(limit: int = 120) -> str:
    root = workspace_root()
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            entries.append(relative(Path(dirpath) / name))
            if len(entries) >= limit:
                return "\n".join(entries) + "\n… [map truncated]"
    return "\n".join(entries)


def relevant_files(query: str, *, max_files: int = 4, per_file_chars: int = 2500) -> list[tuple[str, str]]:
    root = workspace_root()
    wanted = _tokens(query)
    scored: list[tuple[float, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            file = Path(dirpath) / name
            if file.suffix.lower() not in CODE_SUFFIXES:
                continue
            try:
                size = file.stat().st_size
            except OSError:
                continue
            if size == 0 or size > 200_000:
                continue
            rel = relative(file)
            score = 3.0 * len(wanted & _tokens(rel))
            if score == 0 and name.lower() not in {"readme.md", "package.json"}:
                # cheap content peek only for otherwise-unranked files
                try:
                    head = file.read_text(encoding="utf-8", errors="ignore")[:4000]
                except OSError:
                    continue
                score = len(wanted & _tokens(head)) * 0.5
            if name.lower() in {"readme.md", "package.json", "pyproject.toml"}:
                score += 1.5
            if score > 0:
                scored.append((score, file))
    scored.sort(key=lambda item: item[0], reverse=True)

    out: list[tuple[str, str]] = []
    for _, file in scored[:max_files]:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")[:per_file_chars]
        except OSError:
            continue
        out.append((relative(file), text))
    return out


def build_context(query: str, *, max_context_chars: int = 8000) -> ProjectContext:
    tree = project_map()
    files = relevant_files(query)
    context = ProjectContext(tree=tree, files=files, root=str(workspace_root()))
    # Trim from the tail until the block fits the budget.
    while len(context.as_prompt()) > max_context_chars and context.files:
        context.files.pop()
    if len(context.as_prompt()) > max_context_chars:
        context.tree = context.tree[: max_context_chars // 2] + "\n… [map truncated]"
    return context


def history_window(messages: list[dict[str, str]], *, keep: int = 12, max_chars: int = 6000) -> list[dict[str, str]]:
    window = messages[-keep:]
    total = 0
    trimmed: list[dict[str, str]] = []
    for message in reversed(window):
        content = message.get("content", "")
        if total + len(content) > max_chars:
            content = content[: max(200, max_chars - total)] + " …"
        trimmed.append({**message, "content": content})
        total += len(content)
        if total >= max_chars:
            break
    return list(reversed(trimmed))
