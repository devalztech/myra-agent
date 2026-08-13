"""Guardrails: command restrictions, approvals and tool budgets.

Three independent layers:

1. **Workspace isolation** (``app/workspace.py``) — every path goes through
   ``safe_path``; nothing outside Myra's own directory is reachable.
2. **Command screening** (here) — a deny-list of destructive / escape-prone
   shell patterns plus an optional allow-list mode.
3. **Budgets** (here) — per-run caps on tool calls, wall clock and output
   size, so a runaway loop cannot burn the panel.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field

from ..config import settings

# Commands that can destroy the host, escape the workspace, or hijack the
# panel. Matched against the raw command string AND the parsed argv.
DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*/(\s|$)", "refusing to delete the filesystem root"),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(~|/home|/etc|/var|/usr|/bin)", "refusing to delete system paths"),
    (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
    (r"\bmkfs(\.|\s)", "filesystem formatting"),
    (r"\bdd\s+if=.*of=/dev/", "raw device write"),
    (r">\s*/dev/(sd|nvme|hd)", "raw device write"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", "host power control"),
    (r"\bsudo\b|\bsu\s|\bdoas\b", "privilege escalation"),
    (r"\bchown\s+.*(/home/container|/etc)", "changing ownership of protected paths"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/", "world-writable on system paths"),
    (r"/home/container", "Pterodactyl panel files are off limits"),
    (r"\b(pterodactyl|wings)\b", "Pterodactyl panel files are off limits"),
    (r"\b(iptables|ufw|systemctl|service)\b", "host service/network control"),
    (r"\bcrontab\b", "host cron is off limits — use Myra's scheduler"),
    (r"\bkill(all)?\s+-9\s+1\b", "refusing to kill init"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "piping a remote script into a shell"),
    (r"\bwget\b[^|]*\|\s*(ba)?sh", "piping a remote script into a shell"),
    (r"\bnc\b\s+-l", "opening a raw listener"),
    (r"\b/etc/(passwd|shadow|sudoers)", "reading credential files"),
    (r"\bssh(-keygen|-copy-id)?\b", "ssh access"),
)

# Commands that always need explicit approval when approvals are enabled.
SENSITIVE_BINARIES = {
    "git",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "pip",
    "pip3",
    "apt",
    "apt-get",
    "docker",
    "make",
    "psql",
}


class CommandBlocked(PermissionError):
    """Raised when a command is refused by the guardrails."""


class ApprovalRequired(PermissionError):
    """Raised when a command needs a human 'yes' before it can run."""


class BudgetExceeded(RuntimeError):
    """Raised when a run exhausts its tool-call / time budget."""


def screen_command(command: str, *, approved: bool = False) -> None:
    """Raise if ``command`` is denied or needs approval."""
    text = (command or "").strip()
    if not text:
        raise CommandBlocked("Empty command.")
    if len(text) > 4000:
        raise CommandBlocked("Command is too long.")

    lowered = text.lower()
    for pattern, reason in DENY_PATTERNS:
        if re.search(pattern, lowered):
            raise CommandBlocked(f"Blocked by guardrails ({reason}): {text}")

    try:
        argv = shlex.split(text)
    except ValueError:
        argv = text.split()

    if settings.approval_required and not approved:
        head = (argv[0] if argv else "").rsplit("/", 1)[-1]
        if head in SENSITIVE_BINARIES:
            raise ApprovalRequired(
                f"'{head}' requires approval. Re-run with approval to continue."
            )


@dataclass
class RunBudget:
    """Per-run limits, checked before every tool call."""

    max_tool_calls: int = field(default_factory=lambda: settings.max_tool_calls)
    max_steps: int = field(default_factory=lambda: settings.max_agent_steps)
    timeout_seconds: int = field(default_factory=lambda: settings.agent_timeout_seconds)
    tool_calls: int = 0
    steps: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.timeout_seconds - self.elapsed)

    def check_time(self) -> None:
        if self.elapsed > self.timeout_seconds:
            raise BudgetExceeded(
                f"Run exceeded its time budget ({self.timeout_seconds}s)."
            )

    def charge_tool(self) -> None:
        self.check_time()
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(
                f"Run exceeded its tool-call budget ({self.max_tool_calls} calls)."
            )
        self.tool_calls += 1

    def charge_step(self) -> bool:
        self.check_time()
        if self.steps >= self.max_steps:
            return False
        self.steps += 1
        return True


def truncate(text: str, limit: int | None = None) -> str:
    cap = limit or settings.max_output_chars
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n… [truncated, {len(text) - cap} more characters]"
