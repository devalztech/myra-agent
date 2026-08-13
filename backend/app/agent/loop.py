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


def _extract_action(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1).strip())
    start = text.find("{")
    if start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("tool" in parsed or "final" in parsed):
            return parsed
    return None


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
        context = build_context(request)
        blocks.append("Current workspace context:\n" + context.as_prompt())
        return "\n\n".join(blocks)

    # -- run ------------------------------------------------------------
    def run(self, request: str, history: list[dict[str, str]] | None = None) -> Iterator[AgentEvent]:
        started = time.monotonic()
        transcript: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt(request)},
            *history_window(history or []),
            {"role": "user", "content": request},
        ]
        final_text: str | None = None

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
                # The model answered in prose — treat it as the final reply.
                final_text = raw
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
