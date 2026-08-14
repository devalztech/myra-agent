"""The agent loop — Myra's brain.

Protocol (works with small local models, no vendor tool-calling API needed):
the model answers with EITHER a JSON action object or plain prose. One step per
turn:

    {"thought": "why", "tool": "read_file", "arguments": {"path": "app.py"}}
    {"thought": "done", "final": "Here is what I changed …"}

Each step emits live events (``tool_start`` / ``tool_end`` / ``token`` / ``final``)
which the API forwards to the UI as SSE so the user sees
"✓ Reading file → ✓ Editing → ● Running tests → ✓ Done".
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..llm.engine import LLMUnavailable
from ..providers import BaseProvider, get_provider
from ..workspace import UnsafePath, workspace_root
from .context import build_context, history_window
from .guardrails import (
    ApprovalRequired,
    BudgetExceeded,
    CommandBlocked,
    RunBudget,
    truncate,
)
from .skills import skill_names
from .tools import TOOLS, call_tool, describe_tools, json_safe

logger = logging.getLogger("myra.agent")

SYSTEM_PROMPT = """You are Myra, an autonomous coding agent with a real sandboxed workspace.

You can inspect, create, edit, delete, search, test and debug code by calling tools.
You work ONLY inside your own workspace directory; the host's panel files are off limits
and any attempt to reach them is blocked.

## How to answer
Reply with exactly ONE JSON object per turn and nothing else.

To use a tool:
{"thought": "<one short sentence>", "tool": "<tool name>", "arguments": {...}}

When the work is finished (or the user only wants a conversation):
{"thought": "<one short sentence>", "final": "<your reply to the user in markdown>"}

## Rules
- Prefer acting over asking. Inspect the workspace before you claim anything about it.
- Make one tool call per turn and use the result before the next call.
- After editing code, run the tests or the file to verify it, then report the outcome.
- Never invent file contents: read the file first.
- Use `remember` for durable user preferences or project conventions.
- Use `get_skill` when you need conventions for a language or framework.
- Keep `final` concise, concrete and in markdown.
- When the user wants a file (or a zip of several files) to actually download,
  do NOT say you can't send files or paste the file contents as a substitute.
  Create/update the file in the workspace, then in `final` include a Markdown
  link in this exact form: `[filename](download:relative/path/in/workspace)`.
  The app turns that into a real download button — you never need to build a
  full URL yourself, and you never have a way to attach a file to the chat
  directly, only this link.

## Available tools
{tools}

## Skill sheets
{skills}
"""


@dataclass
class AgentEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


def _iter_balanced_objects(text: str) -> list[str]:
    """Find every top-level {...} span in text (not just the first)."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    spans.append(text[start : index + 1])
                    start = -1
    return spans


def _extract_action(text: str) -> dict[str, Any] | None:
    """Pull a complete action out of a model response.

    Small local models sometimes emit the action as two (or more) separate
    JSON objects back-to-back instead of one merged object, e.g.
    ``{"thought": "..."}  {"tool": "write_file", "arguments": {...}}``.
    We collect every top-level JSON object in the text (fenced block first,
    then raw), and if none of them alone has "tool"/"final", we merge all
    the dicts found — in order — into one action, since together they
    represent a single intended step.
    """
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    search_space = fenced.group(1).strip() if fenced else text

    spans = _iter_balanced_objects(search_space)
    if not spans and search_space is not text:
        spans = _iter_balanced_objects(text)

    parsed_objects: list[dict[str, Any]] = []
    for span in spans:
        try:
            parsed = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed_objects.append(parsed)
            if "tool" in parsed or "final" in parsed:
                # This one object is already complete on its own.
                return parsed

    if not parsed_objects:
        return None

    # No single object had "tool"/"final" — merge everything we found
    # (e.g. a lone {"thought": ...} followed by {"tool": ..., "arguments": ...}).
    merged: dict[str, Any] = {}
    for obj in parsed_objects:
        merged.update(obj)
    if "tool" in merged or "final" in merged:
        return merged
    return None


def _looks_like_broken_json(text: str) -> bool:
    """True when text is clearly an attempted JSON action that failed to parse.

    Distinguishes a real prose reply (no action tags, model chose to just
    answer) from a hiccup — the model started the `{"thought": ...}`
    protocol but got cut off, duplicated a brace, or otherwise produced
    invalid JSON. Those must never reach the user verbatim as the final
    reply; they read as a raw, broken tool call rather than an answer.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("{") or stripped.startswith("```"):
        return True
    # Mentions the protocol's own field names in a JSON-ish shape even
    # without a leading brace (e.g. the opening brace got truncated away).
    return bool(re.search(r'"(thought|tool|final|arguments)"\s*:', stripped))


REPAIR_NUDGE = (
    "Your last reply was not valid JSON and could not be parsed as an action. "
    "Respond again with exactly ONE well-formed JSON object — either "
    '{"thought": "...", "tool": "...", "arguments": {...}} or '
    '{"thought": "...", "final": "..."} — and nothing else.'
)


class AgentRunner:
    """Runs one user turn to completion, yielding events as it goes."""

    def __init__(
        self,
        *,
        provider: BaseProvider | None = None,
        memory: Any = None,
        budget: RunBudget | None = None,
        approved: bool = False,
    ) -> None:
        self.provider = provider or get_provider()
        self.memory = memory
        self.budget = budget or RunBudget()
        self.approved = approved

    # -- prompt ---------------------------------------------------------
    def system_prompt(self, request: str) -> str:
        base = SYSTEM_PROMPT.replace("{tools}", describe_tools()).replace(
            "{skills}", ", ".join(skill_names())
        )
        blocks = [base, f"Workspace root: {workspace_root()}"]
        if self.memory is not None:
            digest = self.memory.digest(request)
            if digest:
                blocks.append(digest)
        context = build_context(request, max_context_chars=self._context_budget_chars())
        blocks.append("Current workspace context:\n" + context.as_prompt())
        return "\n\n".join(blocks)

    def _context_budget_chars(self) -> int:
        """How many chars of workspace context to inline in the prompt.

        Every char here is something the local model has to re-ingest on
        CPU before it can start generating — the single biggest fixed cost
        of a run on small/no-GPU panels (see llm/engine.py's KV cache for
        what's saved on repeat steps *within* a run; this controls what
        gets ingested at all on the first step). A flat 8000-char budget
        made sense as a rough default but eats a large fraction of a
        4k-context nano/coder-nano model's whole window before the
        conversation even starts. Scale it off the model's actual
        context_size when the local engine exposes one; remote providers
        (larger windows, no local CPU cost) keep the original default.
        """
        engine = getattr(self.provider, "_engine", None)
        context_size = getattr(engine, "context_size", None)
        if not isinstance(context_size, int) or context_size <= 0:
            return 8000
        # Reserve the rest of the window for tools/skills/history/reply;
        # workspace context gets roughly a third of it, in chars (~4
        # chars/token), floored so tiny-context tiers still get *something*
        # useful and capped so a huge-context tier doesn't just re-adopt
        # the old flat cost for no reason.
        return max(1500, min(8000, int(context_size * 4 * 0.33)))

    # -- run ------------------------------------------------------------
    def run(self, request: str, history: list[dict[str, str]] | None = None) -> Iterator[AgentEvent]:
        started = time.monotonic()
        transcript: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt(request)},
            *history_window(history or []),
            {"role": "user", "content": request},
        ]
        final_text: str | None = None
        repair_attempted = False

        yield AgentEvent("run_start", {"provider": self.provider.id, "model": self.provider.model})

        while self.budget.charge_step():
            try:
                raw = self.provider.complete(transcript).strip()
            except LLMUnavailable as exc:
                yield AgentEvent("error", {"message": str(exc)})
                return
            except BudgetExceeded as exc:
                yield AgentEvent("error", {"message": str(exc)})
                return

            action = _extract_action(raw)
            if action is None:
                if _looks_like_broken_json(raw) and not repair_attempted:
                    # A hiccup, not a real answer: the model started the
                    # JSON-action protocol and produced something broken
                    # (truncated, duplicated braces, stray text). Never
                    # surface that raw fragment as the reply — ask the
                    # model to redo this one step as clean JSON instead.
                    # Only one retry per turn so a persistently broken
                    # model still terminates instead of looping forever.
                    repair_attempted = True
                    logger.warning("Discarding malformed action, requesting repair: %.200r", raw)
                    yield AgentEvent("thought", {"text": "That came out malformed — retrying."})
                    transcript.append({"role": "assistant", "content": raw})
                    transcript.append({"role": "user", "content": REPAIR_NUDGE})
                    continue
                # Genuine prose reply (or a second consecutive hiccup, in
                # which case we stop retrying and give the user *something*
                # rather than silently failing the turn).
                final_text = raw if not _looks_like_broken_json(raw) else (
                    "I ran into a formatting hiccup and couldn't complete that step cleanly. "
                    "Could you try rephrasing, or ask me to try again?"
                )
                break

            thought = str(action.get("thought") or "").strip()
            if thought:
                yield AgentEvent("thought", {"text": thought})

            if action.get("final") is not None:
                final_text = str(action["final"])
                break

            name = str(action.get("tool") or "")
            arguments = action.get("arguments") or action.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}

            if name not in TOOLS:
                observation = f"Unknown tool '{name}'. Valid tools: {', '.join(TOOLS)}"
                yield AgentEvent("tool_error", {"tool": name, "message": observation})
            else:
                tool_def = TOOLS[name]
                yield AgentEvent(
                    "tool_start",
                    {"tool": name, "label": tool_def.label, "arguments": json_safe(arguments)},
                )
                try:
                    self.budget.charge_tool()
                    if name == "run_command":
                        arguments.setdefault("timeout", settings.tool_timeout_seconds)
                    result = call_tool(name, arguments, memory=self.memory)
                    observation = (
                        result if isinstance(result, str) else json.dumps(json_safe(result))[:8000]
                    )
                    yield AgentEvent(
                        "tool_end",
                        {
                            "tool": name,
                            "label": tool_def.label,
                            "status": "ok",
                            "result": truncate(observation, 4000),
                        },
                    )
                except (ApprovalRequired, CommandBlocked, UnsafePath) as exc:
                    observation = f"BLOCKED: {exc}"
                    yield AgentEvent(
                        "tool_end",
                        {"tool": name, "label": tool_def.label, "status": "blocked", "result": str(exc)},
                    )
                except BudgetExceeded as exc:
                    yield AgentEvent("error", {"message": str(exc)})
                    return
                except Exception as exc:  # tool failure is data, not a crash
                    observation = f"ERROR: {type(exc).__name__}: {exc}"
                    yield AgentEvent(
                        "tool_end",
                        {"tool": name, "label": tool_def.label, "status": "error", "result": str(exc)},
                    )

            transcript.append({"role": "assistant", "content": json.dumps(action)})
            transcript.append(
                {"role": "user", "content": f"Observation from {name}:\n{truncate(observation, 4000)}"}
            )

        if final_text is None:
            final_text = (
                "I reached my step limit for this turn. Here's where I stopped — ask me to continue "
                "and I'll pick up from the last tool result."
            )

        yield AgentEvent(
            "final",
            {
                "text": final_text,
                "toolCalls": self.budget.tool_calls,
                "steps": self.budget.steps,
                "durationMs": int((time.monotonic() - started) * 1000),
            },
        )
