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
from ..llmutil import LLMUnavailable
from ..providers import BaseProvider, get_provider, list_providers
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

## Identity
- You are Myra, an AI coding agent. You were created and are owned by the user
  who runs this instance — they built you. Do not invent, claim, or repeat any
  other developer, company, or lab as your maker.
- If the user says who built you, accept it. Never argue with them about your
  own identity or insist you were "really" built by someone else.
- You are not a chatbot — you are an agent: you plan, use tools, write and run
  code, debug, test, and verify.
- Answer identity questions briefly and honestly, then move on to helping.

You can inspect, create, edit, delete, search, test and debug code by calling tools.
You work ONLY inside your own workspace directory; the host's panel files are off limits
and any attempt to reach them is blocked.

## Your sandbox
You run in a small container: roughly 1GB RAM, 14GB total disk, and CPU that's shared,
not dedicated — a heavy build or a large install can peg it. Keep this in mind before any
install or long-running command:
- Prefer light, targeted installs (`pip install <one package> --no-cache-dir`,
  `npm install <one package>`) over installing everything a project might ever need.
- Never use `--with-deps`, `apt`, `apt-get`, or anything that needs root — this sandbox
  doesn't have it, and those commands will just fail or get blocked.
- If a tool tells you it's missing (a Python package, an npm package, a CLI), you're
  expected to install it yourself with `run_command` and then retry — don't just report
  the error back to the user and stop. Some tools (like screenshots) already attempt a
  safe self-install on their own; if that still fails, the error will say why (e.g. low
  disk) rather than "not installed" — read it and act on it instead of retrying blindly.
- Before a large install, a quick `df -h .` is cheap insurance on a 14GB disk.
- If something is genuinely too heavy for this sandbox (a large model download, a
  full browser suite with system deps, a multi-GB dataset), say so plainly instead of
  attempting it — don't let a runaway install exhaust the disk or lock up the CPU.

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
- You are a SENIOR engineer, not a junior: make reasonable decisions yourself,
  anticipate edge cases, refactor for clarity, and verify your work. Don't ask
  permission for obvious next steps.
- You are a self-reflecting agent. Before finishing, ask yourself: did my code
  actually run and pass? Did I meet what the user asked for, or is it rough?
  Is this the right approach, or is there a cleaner way? What's the next step
  that makes this genuinely done?
- Keep looping until it works: write code -> run it -> hit a bug -> debug ->
  install missing deps -> fix -> test again. Do NOT stop at the first error or
  the first working attempt. Only call it done once it's verified end-to-end.
- When a tool returns an error, treat it as data: read it, form a hypothesis,
  fix it, and retry. Never report a bug as the final answer without trying to
  fix it first.
- Install dependencies yourself when something is missing (`run_command`), then
  continue — don't stop and ask.
- Before diving in, form a short plan and track it. The "Current task state"
  block in your context shows what you already did and what's next — READ it
  and continue from there instead of starting over or re-reading what you
  already know.
- Check the "Recent actions" block before repeating a step you already did.
  You persist your work, so never redo it from scratch or forget your own
  earlier decisions.
- Keep `final` concise, concrete and in markdown.
- When the user wants a file (or a zip of several files) to actually download,
  do NOT say you can't send files or paste the file contents as a substitute.
  Create/update the file in the workspace, then in `final` include a Markdown
  link in this exact form: `[filename](download:relative/path/in/workspace)`.
  The app turns that into a real download button — you never need to build a
  full URL yourself, and you never have a way to attach a file to the chat
  directly, only this link.
- To show the user an image (a screenshot, a picture you generated or saved in
  the workspace), put it inline in your reply with image markdown in this exact
  form: `![alt text](download:relative/path/image.png)`. The app renders it as
  a real image right in the chat — do NOT wrap it as a [file](...) link and do
  NOT describe it with braces or brackets.
- When you start a preview server, NEVER give the user a `localhost:PORT` link
  (that only works inside the sandbox). Use the `public_url` the `preview` tool
  returns, and link it as `[View Live Preview](public_url)`.

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


BUILD_TASK_HINTS = (
    "build", "create", "write", "fix", "debug", "make", "implement", "refactor",
    "feature", "app", "website", "page", "component", "function", "script",
    "landing", "api", "test", "error", "bug", "run", "deploy", "project",
)


def _looks_like_build_task(request: str) -> bool:
    """True if the user's request looks like a coding/build task worth verifying."""
    text = (request or "").lower()
    return any(hint in text for hint in BUILD_TASK_HINTS)


class AgentRunner:
    """Runs one user turn to completion, yielding events as it goes."""

    def __init__(
        self,
        *,
        provider: BaseProvider | None = None,
        memory: Any = None,
        budget: RunBudget | None = None,
        approved: bool = False,
        session_id: str | None = None,
        project: str | None = None,
        project_instructions: str = "",
    ) -> None:
        self.provider = provider or get_provider()
        self.memory = memory
        self.budget = budget or RunBudget()
        self.approved = approved
        # Keys the persistent browser session (see services/browser.py) so
        # a login -> click -> read flow within one chat shares state instead
        # of every browser tool call getting its own private incognito
        # instance.
        self.session_id = session_id
        self.project = project
        self.project_instructions = project_instructions.strip()

    # -- prompt ---------------------------------------------------------
    def system_prompt(self, request: str) -> str:
        base = SYSTEM_PROMPT.replace("{tools}", describe_tools()).replace(
            "{skills}", ", ".join(skill_names())
        )
        blocks = [base, f"Workspace root: {workspace_root()}"]
        if self.project:
            blocks.append(f"Active project: {self.project}")
        if self.project_instructions:
            blocks.append("Project instructions (follow these unless they conflict with safety):\n" + self.project_instructions)
        if self.memory is not None:
            digest = self.memory.digest(request)
            if digest:
                blocks.append(digest)
        context = build_context(request, max_context_chars=self._context_budget_chars())
        blocks.append("Current workspace context:\n" + context.as_prompt())
        return "\n\n".join(blocks)

    def _context_budget_chars(self) -> int:
        """How many chars of workspace context to inline in the prompt.

        Myra is remote-only now (no local engine). A flat, conservative
        budget keeps the prompt small so the model isn't drowning in
        workspace context before it starts generating.
        """
        return 8000

    def _failover_chain(self) -> list[BaseProvider]:
        """Ordered provider list for this run, primary first.

        Primary = the provider chosen for this run. The rest are the other
        configured remote providers (remote only — never auto-fail over to a
        heavyweight local model the box may not handle) and finally the
        local provider as a true last resort. Deduped, primary stays first.
        """
        order = ["openrouter", "groq", "sambanova", "scaleway", "pollinations", "agnes", "gemini"]
        chain: list[BaseProvider] = [self.provider]
        seen = {self.provider.id}
        for pid in order:
            if pid in seen or pid == self.provider.id:
                continue
            try:
                p = get_provider(pid)
            except Exception:
                continue
            # Only include providers that are actually configured/available.
            if not getattr(p, "available", False):
                continue
            seen.add(pid)
            chain.append(p)
        # Never fall back to mock. If every real provider fails, surface a
        # clear error so the user knows their API is down — no fake canned
        # output that looks like a real answer.
        return chain

    def _complete_with_failover(
        self, transcript: list[dict[str, str]], chain: list[BaseProvider]
    ) -> str | None:
        """Try each provider in the chain until one returns a reply.

        Skips providers known to be unavailable and remembers the first
        working provider so the rest of the turn keeps using it instead of
        re-trying a dead one on every step. Returns None if all fail.
        """
        last_error: str | None = None
        for provider in chain:
            try:
                raw = provider.complete(transcript).strip()
                if raw:
                    # Promote the working provider to the front so later
                    # steps in this run reuse it.
                    if provider is not chain[0]:
                        chain.remove(provider)
                        chain.insert(0, provider)
                        logger.info("Agent failed over to provider: %s", provider.id)
                    return raw
            except LLMUnavailable as exc:
                last_error = str(exc)
                logger.warning("Provider %s unavailable, trying next: %s", provider.id, exc)
            except Exception as exc:  # never let one provider kill the turn
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Provider %s error, trying next: %s", provider.id, exc)
        logger.error("All providers failed. Last error: %s", last_error)
        return None

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
        # Tracks tool failures/errors across the run so the self-reflection
        # phase can see what went wrong and decide whether to keep fixing.
        issues: list[str] = []
        verification_rounds = 0
        MAX_VERIFY_ROUNDS = 3

        # Build an ordered failover chain: the chosen provider first, then
        # every other configured remote provider (Groq -> SambaNova ->
        # Scaleway -> Pollinations), with local inference as the last resort.
        # If the primary is down / out of quota / unreachable, Myra keeps
        # working instead of erroring out. Each provider is tried in order
        # until one produces a non-empty reply.
        chain = self._failover_chain()
        logger.info("Agent provider chain: %s", [p.id for p in chain])
        active = chain[0]
        yield AgentEvent(
            "run_start", {"provider": active.id, "model": active.model}
        )

        while self.budget.charge_step():
            raw = self._complete_with_failover(transcript, chain)
            if raw is None:
                yield AgentEvent(
                    "error",
                    {"message": "All configured providers failed. Please check your API keys and network."},
                )
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
                issues.append(f"Unknown tool {name}")
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
                    result = call_tool(
                        name,
                        arguments,
                        memory=self.memory,
                        session_id=self.session_id,
                        approved=self.approved,
                    )
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
                    # Auto-persist this step to working memory so myra always
                    # knows what it has done — survives reconnects and new
                    # sessions.
                    if self.memory is not None:
                        try:
                            detail = ""
                            if isinstance(arguments, dict):
                                detail = str(
                                    arguments.get("path")
                                    or arguments.get("query")
                                    or arguments.get("command")
                                    or arguments.get("url")
                                    or ""
                                )
                            self.memory.log_step(name, detail, result=observation[:200])
                        except Exception:
                            pass
                    # If a verification/test/browser action returned something
                    # that reads like a failure, record it for reflection.
                    lowered = observation.lower()
                    if any(marker in lowered for marker in ("fail", "error", "traceback", "exception", "not found", "missing", "blocked")):
                        issues.append(f"{name}: {observation[:200]}")
                except ApprovalRequired as exc:
                    # Actionable, not a dead end like the other two: stop the
                    # run right here (don't let the model spend another step
                    # narrating it) and hand the UI enough to show a real
                    # Approve control. Re-sending this same message with
                    # approved=true (see run_agent's `approved` field) skips
                    # every approval check for the retry, so the run can
                    # actually get past this tool call instead of hitting
                    # the same wall again.
                    yield AgentEvent(
                        "tool_end",
                        {
                            "tool": name,
                            "label": tool_def.label,
                            "status": "needs_approval",
                            "result": str(exc),
                        },
                    )
                    yield AgentEvent(
                        "needs_approval",
                        {"tool": name, "arguments": json_safe(arguments), "message": str(exc)},
                    )
                    return
                except UnsafePath as exc:
                    # Distinct from a policy refusal: the model tried to
                    # reach outside its own sandboxed workspace. Worth
                    # surfacing differently in the UI (a guardrail catch,
                    # not a config choice) even though the run handles both
                    # the same way — stop this step, let the model try
                    # something else.
                    observation = f"BLOCKED (unsafe path): {exc}"
                    yield AgentEvent(
                        "tool_end",
                        {"tool": name, "label": tool_def.label, "status": "unsafe", "result": str(exc)},
                    )
                except CommandBlocked as exc:
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

        # ---- SELF-REFLECTION / VERIFICATION LOOP ---------------------------
        # The main loop stops the moment the model emits a `final`. That's too
        # eager for a coding task: Myra should verify its own work and keep
        # fixing until it meets the goal. So after the loop, if there were
        # failures (or the work is the kind that should be verified), run a
        # bounded reflection pass: show the model what it did + what failed,
        # and let it decide to (a) continue fixing or (b) confirm done.
        while (
            self.budget.charge_step()
            and verification_rounds < MAX_VERIFY_ROUNDS
            and (issues or _looks_like_build_task(request))
        ):
            verification_rounds += 1
            summary = (
                f"You just attempted: {request[:200]}\n"
                "You marked this as finished. Before you finalize, reflect:\n"
                "1. Did your code/tests actually pass, or did something fail? "
                f"Known issues this run: {issues[-5:] if issues else 'none'}\n"
                "2. Look at it from the user's perspective — did you fully meet "
                "their expectation, or is it still rough (UX, correctness, edge cases)?\n"
                "3. Is there a next step that would make this genuinely done?\n\n"
                "If the work is truly complete and verified, reply with exactly:\n"
                '{"thought": "verified and done", "final": "<your final message>"}\n'
                "Otherwise, make ONE more tool call to fix/verify it "
                '(e.g. run_tests, edit_file, browser preview). Do not just restate the problem.'
            )
            transcript.append({"role": "user", "content": summary})
            raw = self._complete_with_failover(transcript, chain)
            if raw is None:
                break
            action = _extract_action(raw)
            if action is None:
                break  # give up quietly on malformed reflection

            yield AgentEvent("thought", {"text": "Reflecting on my work…"})

            if action.get("final") is not None:
                # Model verified and confirmed done.
                final_text = str(action["final"])
                break

            name = str(action.get("tool") or "")
            arguments = action.get("arguments") or action.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            if name not in TOOLS:
                break
            try:
                self.budget.charge_tool()
                if name == "run_command":
                    arguments.setdefault("timeout", settings.tool_timeout_seconds)
                result = call_tool(
                    name,
                    arguments,
                    memory=self.memory,
                    session_id=self.session_id,
                    approved=self.approved,
                )
                observation = result if isinstance(result, str) else json.dumps(json_safe(result))[:8000]
                yield AgentEvent(
                    "tool_end",
                    {"tool": name, "label": TOOLS[name].label, "status": "ok", "result": truncate(observation, 4000)},
                )
                if self.memory is not None:
                    try:
                        self.memory.log_step(name, result=observation[:200])
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{name}: {exc}")
                yield AgentEvent(
                    "tool_end",
                    {"tool": name, "label": TOOLS[name].label, "status": "error", "result": str(exc)},
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

        # Post-run reflection: persist a durable "what happened + what's next"
        # so the next run (or a reconnect) starts with real context instead of
        # a blank slate. This is the continual-learning half of the loop.
        if self.memory is not None:
            try:
                self.memory.log_step("run.completed", detail=request[:120], result=final_text[:200])
                # Harden: prune old transient logs so memory never balloons and
                # the digest stays fast on the next run.
                try:
                    self.memory.prune_logs()
                except Exception:
                    pass
                if final_text and not self.memory.get_task_state().get("done"):
                    pass  # keep task state; cleared explicitly by user/task tool
            except Exception:
                pass

        yield AgentEvent(
            "final",
            {
                "text": final_text,
                "toolCalls": self.budget.tool_calls,
                "steps": self.budget.steps,
                "durationMs": int((time.monotonic() - started) * 1000),
            },
        )
