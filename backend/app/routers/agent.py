"""Agent API: streaming runs, providers, memory, tasks, tools, workspace."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import AsyncIterator
from datetime import timedelta
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.guardrails import RunBudget
from ..agent.loop import AgentRunner
from ..agent.memory import MemoryStore
from ..agent.skills import SKILLS, skill_names
from ..agent.tools import TOOLS
from ..config import settings
from ..database import SessionLocal, get_db
from ..deps import get_current_user, get_owned_session
from ..llmutil import title_from_message
from ..models import AgentEventRow, ChatMessage, Memory, ScheduledTask, User, UserSettings, _uuid, utcnow
from ..providers import get_provider, list_providers
from ..scheduler import run_task_now
from ..workspace import UnsafePath, relative, safe_path, workspace_info, workspace_root

logger = logging.getLogger("myra.agent.api")

router = APIRouter(tags=["agent"])


# --------------------------------------------------------------------------
# settings / providers
# --------------------------------------------------------------------------


class SettingsPayload(BaseModel):
    provider: str | None = None
    approvalRequired: bool | None = None
    maxToolCalls: int | None = Field(default=None, ge=0, le=200)
    agentMode: bool | None = None


class ProviderConfigPayload(BaseModel):
    provider: str
    apiKey: str | None = None
    baseUrl: str | None = None
    model: str | None = None


def _settings_row(db: Session, user: User) -> UserSettings:
    row = db.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id, provider=settings.default_provider)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _settings_out(row: UserSettings) -> dict[str, object]:
    return {
        "provider": row.provider,
        "approvalRequired": row.approval_required,
        "maxToolCalls": row.max_tool_calls or settings.max_tool_calls,
        "agentMode": row.agent_mode,
        "limits": {
            "maxToolCalls": settings.max_tool_calls,
            "maxSteps": settings.max_agent_steps,
            "toolTimeoutSeconds": settings.tool_timeout_seconds,
            "agentTimeoutSeconds": settings.agent_timeout_seconds,
        },
    }


def _mask_key(key: str | None) -> str | None:
    if not key:
        return None
    if len(key) <= 8:
        return "••••"
    return f"{key[:4]}…{key[-4:]}"


@router.get("/settings/providers")
def read_provider_configs(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    row = _settings_row(db, user)
    configs = dict(row.provider_configs or {})
    # Return masked keys so the UI can show "set" without leaking secrets.
    masked: dict[str, object] = {}
    for pid, cfg in configs.items():
        masked[pid] = {
            "apiKey": _mask_key(cfg.get("api_key")) if isinstance(cfg, dict) else None,
            "baseUrl": cfg.get("base_url") if isinstance(cfg, dict) else None,
            "model": cfg.get("model") if isinstance(cfg, dict) else None,
            "hasKey": bool(isinstance(cfg, dict) and cfg.get("api_key")),
        }
    return {"providers": masked}


@router.put("/settings/providers/{provider_id}")
def update_provider_config(
    provider_id: str,
    payload: ProviderConfigPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    known = {p["id"] for p in list_providers()}
    if provider_id not in known:
        raise HTTPException(status_code=400, detail="Unknown provider.")
    row = _settings_row(db, user)
    configs = dict(row.provider_configs or {})
    current = dict(configs.get(provider_id, {}) or {})
    if payload.apiKey is not None:
        current["api_key"] = payload.apiKey
    if payload.baseUrl is not None:
        current["base_url"] = payload.baseUrl
    if payload.model is not None:
        current["model"] = payload.model
    configs[provider_id] = current
    row.provider_configs = configs
    db.commit()
    return {"provider": provider_id, "saved": True}


@router.get("/providers")
def providers() -> dict[str, object]:
    return {"providers": list_providers(), "default": settings.default_provider}


@router.get("/settings")
def read_settings(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    return _settings_out(_settings_row(db, user))


@router.patch("/settings")
def update_settings(
    payload: SettingsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    row = _settings_row(db, user)
    if payload.provider is not None:
        known = {p["id"] for p in list_providers()}
        if payload.provider not in known:
            raise HTTPException(status_code=400, detail="Unknown provider.")
        row.provider = payload.provider
    if payload.approvalRequired is not None:
        row.approval_required = payload.approvalRequired
    if payload.maxToolCalls is not None:
        row.max_tool_calls = payload.maxToolCalls
    if payload.agentMode is not None:
        row.agent_mode = payload.agentMode
    db.commit()
    db.refresh(row)
    return _settings_out(row)


@router.get("/tools")
def tools() -> dict[str, object]:
    return {
        "tools": [
            {
                "name": t.name,
                "label": t.label,
                "description": t.description,
                "mutates": t.mutates,
                "parameters": t.parameters,
            }
            for t in TOOLS.values()
        ]
    }


@router.get("/skills")
def skills() -> dict[str, object]:
    return {"skills": [{"name": name, "content": SKILLS[name]} for name in skill_names()]}


# --------------------------------------------------------------------------
# agent run (SSE)
# --------------------------------------------------------------------------

# Explicit-stop registry, keyed by session id. A bare dropped connection
# (phone locks, wifi drops, tab closed) is NOT in here — the run keeps going
# and finishes unattended so the user comes back to a completed task instead
# of one abandoned half-way through. Only a deliberate call to
# POST /sessions/{id}/stop adds an entry, which the running generator polls
# for and honours as "the user actually asked me to stop." Process-local by
# design: a run and its Stop button always live in the same process.
_stop_requested: set[str] = set()
_stop_lock = threading.Lock()


def _request_stop(session_id: str) -> None:
    with _stop_lock:
        _stop_requested.add(session_id)


def _consume_stop(session_id: str) -> bool:
    with _stop_lock:
        return session_id in _stop_requested


def _clear_stop(session_id: str) -> None:
    with _stop_lock:
        _stop_requested.discard(session_id)


class AgentRunPayload(BaseModel):
    content: str = Field(min_length=1, max_length=16000)
    provider: str | None = None
    approved: bool = False


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/sessions/{session_id}/stop", status_code=status.HTTP_202_ACCEPTED)
def stop_agent(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    """Explicit stop, distinct from a dropped connection.

    A closed SSE stream alone no longer halts a run (see run_agent below) —
    it finishes the task in the background so a flaky connection can't
    strand it mid-edit. This is the only thing that actually interrupts a
    run early, e.g. from the UI's Stop button.
    """
    get_owned_session(session_id, db, user)
    _request_stop(session_id)
    return {"stopped": session_id}


@router.post("/sessions/{session_id}/agent")
def run_agent(
    session_id: str,
    payload: AgentRunPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    chat_session = get_owned_session(session_id, db, user)
    prompt = payload.content.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    history = [{"role": m.role, "content": m.content} for m in chat_session.messages]
    user_message = ChatMessage(session_id=chat_session.id, role="user", content=prompt)
    db.add(user_message)
    if not chat_session.messages:
        chat_session.title = title_from_message(prompt)
    chat_session.updated_at = utcnow()
    db.commit()
    db.refresh(user_message)

    row = _settings_row(db, user)
    provider_id = payload.provider or row.provider
    user_message_out = {
        "id": user_message.id,
        "role": "user",
        "content": user_message.content,
        "createdAt": user_message.created_at.isoformat(),
    }
    session_out = {"id": chat_session.id, "title": chat_session.title}
    budget = RunBudget(max_tool_calls=row.max_tool_calls or settings.max_tool_calls)
    # Scalars must be read while the request-scoped session is still open: the
    # ORM instances detach once the generator starts streaming.
    user_id = user.id

    async def stream() -> AsyncIterator[str]:
        # A fresh DB session: the request-scoped one closes when the generator
        # starts streaming outside the dependency scope.
        local_db = SessionLocal()
        try:
            provider = get_provider(provider_id)
            # Apply the user's saved per-provider config (key/base-url/model).
            user_cfg = (row.provider_configs or {}).get(provider_id, {})
            if user_cfg and hasattr(provider, "configure"):
                provider.configure(
                    api_key=user_cfg.get("api_key"),
                    base_url=user_cfg.get("base_url"),
                    model=user_cfg.get("model"),
                )
            yield _sse({"type": "session", **session_out})
            yield _sse({"type": "user_message", "message": user_message_out})

            events: list[dict[str, object]] = []
            final_text = ""
            client_gone = False
            user_stopped = False
            pending_approval: dict[str, object] | None = None
            run_id = _uuid()

            # The agent loop is a synchronous generator and a single tool call
            # (browser/screenshot/command) can take 30-60s. If we consume it
            # directly on the event-loop thread, no SSE bytes flow during those
            # calls — proxies, Cloudflare and the panel all treat a silent
            # stream as dead and kill the connection mid-run ("connection lost
            # while myra works"). So run the loop on a plain worker thread and
            # bridge its events into the async generator through a queue. The
            # generator never blocks for more than HEARTBEAT_INTERVAL, emitting
            # a keep-alive ping whenever the worker is quiet — which keeps the
            # stream alive through long tool calls and flaky networks.
            HEARTBEAT_INTERVAL = 15  # seconds between keep-alive pings
            event_queue: "queue.Queue[object]" = queue.Queue(maxsize=256)
            worker_done = threading.Event()

            def _run_loop() -> None:
                # The agent loop runs on a worker thread, so it needs its OWN
                # DB session for memory/tool writes. Sharing local_db across
                # two threads causes SQLAlchemy's "concurrent operations are
                # not permitted" — the event-loop thread writes live events to
                # local_db while the worker writes memory to the same session.
                worker_db = SessionLocal()
                try:
                    worker_memory = MemoryStore(db=worker_db, user_id=user_id)
                    worker_runner = AgentRunner(
                        provider=provider,
                        memory=worker_memory,
                        budget=budget,
                        approved=payload.approved,
                        session_id=session_id,
                    )
                    for event in worker_runner.run(prompt, history):
                        # If the user asked to stop, stop persisting/forwarding
                        # new events but let the current tool call finish, then
                        # stop early.
                        if _consume_stop(session_id):
                            break
                        event_queue.put(("event", event))
                except Exception as exc:  # noqa: BLE001
                    event_queue.put(("error", exc))
                finally:
                    worker_db.close()
                    worker_done.set()
                    event_queue.put(("done", None))

            threading.Thread(target=_run_loop, daemon=True, name="myra-agent-loop").start()

            # --- dedicated persister thread ---------------------------------
            # Live-persisting every event with its own commit on the event-loop
            # thread was the bottleneck: each event blocked the SSE stream and
            # contended with the worker thread's SQLite writes (SQLite allows
            # one writer at a time), which is exactly the "slow" the user saw.
            # Instead, the event-loop just enqueues events (non-blocking) and a
            # single persister thread flushes them in batches using its own DB
            # session. The SSE stream never touches the DB on the hot path.
            persist_queue: "queue.Queue[dict[str, object]]" = queue.Queue(maxsize=1024)
            persist_done = threading.Event()

            def _persister() -> None:
                persist_db = SessionLocal()
                batch: list[dict[str, object]] = []
                try:
                    while True:
                        # Wait up to 0.25s, then flush whatever accumulated.
                        try:
                            batch.append(persist_queue.get(timeout=0.25))
                        except queue.Empty:
                            pass
                        if persist_done.is_set() and persist_queue.empty():
                            break
                        if not batch:
                            continue
                        # Drop the flush sentinel — it's a control message, not
                        # a real agent event.
                        batch = [d for d in batch if d.get("type") != "flush"]
                        if not batch:
                            continue
                        try:
                            for data in batch:
                                persist_db.add(
                                    AgentEventRow(
                                        session_id=session_id,
                                        message_id=None,
                                        type=str(data.get("type", "event")),
                                        payload=json.dumps({**data, "run_id": run_id})[:8000],
                                    )
                                )
                            persist_db.commit()
                        except Exception:  # noqa: BLE001
                            persist_db.rollback()
                        batch.clear()
                finally:
                    persist_db.close()

            persister_thread = threading.Thread(
                target=_persister, daemon=True, name="myra-event-persister"
            )
            persister_thread.start()

            while True:
                if worker_done.is_set() and event_queue.empty():
                    break  # worker finished and everything was consumed
                try:
                    kind, value = event_queue.get(timeout=HEARTBEAT_INTERVAL)
                except queue.Empty:
                    # Worker still busy (long tool call) — send keep-alive so
                    # the connection isn't torn down by an idle proxy/timeout.
                    if not client_gone:
                        yield _sse({"type": "ping"})
                    continue

                if kind == "error":
                    logger.exception("Agent run failed", exc_info=value)
                    if not client_gone:
                        yield _sse({"type": "error", "message": f"{type(value).__name__}: {value}"})
                    break
                if kind == "done":
                    break

                event = value
                data = event.dict()
                events.append(data)
                # Hand the event to the persister thread (non-blocking). Live
                # events let a dropped/reconnected user (resyncSession -> GET
                # /sessions/{id}/events) see the in-progress trace; the run_id
                # tags the whole batch.
                try:
                    persist_queue.put_nowait(data)
                except queue.Full:
                    pass  # drop the odd event under extreme load; never block the stream
                if event.type == "final":
                    final_text = str(event.data.get("text", ""))
                if event.type == "needs_approval":
                    pending_approval = {
                        "tool": event.data.get("tool"),
                        "arguments": event.data.get("arguments"),
                        "message": event.data.get("message"),
                    }
                # A dropped/closed connection (phone locked, tab closed, wifi
                # blip) does NOT stop the run — Myra keeps working and the
                # full step-by-step trace is saved below via AgentEventRow,
                # so GET /sessions/{id}/events replays everything that
                # happened once the user is back online. Only an EXPLICIT
                # stop (POST /sessions/{id}/stop, wired to the UI's Stop
                # button) breaks the loop early.
                if not client_gone and await request.is_disconnected():
                    client_gone = True
                if not client_gone:
                    yield _sse(data)
                if _consume_stop(session_id):
                    user_stopped = True

            # Tell the persister to drain and finish before we write the final
            # assistant message, so the live trace is complete on disk. It
            # exits once persist_done is set and the queue is empty; join it
            # (bounded) to guarantee everything landed before the assistant row.
            persist_done.set()
            try:
                # Wake a possibly-blocked get() so it re-checks the done flag.
                persist_queue.put_nowait({"type": "flush"})
            except queue.Full:
                pass
            persister_thread.join(timeout=5)

            assistant = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=(
                    final_text
                    or (
                        f"Waiting for approval to run: {pending_approval.get('tool')}"
                        if pending_approval
                        else None
                    )
                    or ("(stopped)" if user_stopped else "(no reply)")
                ),
            )
            local_db.add(assistant)
            local_db.commit()
            local_db.refresh(assistant)
            stored = local_db.get(ChatMessage, assistant.id)
            local_db.commit()
            if client_gone:
                return  # nothing left listening — the trace is saved for replay
            yield _sse(
                {
                    "type": "done",
                    "message": {
                        "id": assistant.id,
                        "role": "assistant",
                        "content": assistant.content,
                        "createdAt": (stored or assistant).created_at.isoformat(),
                    },
                    **({"pendingApproval": pending_approval} if pending_approval else {}),
                }
            )
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            logger.exception("Agent run failed")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            _clear_stop(session_id)
            local_db.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    get_owned_session(session_id, db, user)
    rows = db.scalars(
        select(AgentEventRow)
        .where(AgentEventRow.session_id == session_id)
        .order_by(AgentEventRow.created_at)
    )
    out = []
    for row in rows:
        try:
            payload = json.loads(row.payload)
        except json.JSONDecodeError:
            payload = {"type": row.type}
        out.append({"messageId": row.message_id, **payload})
    return {"events": out}


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------


class MemoryPayload(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="preference", max_length=32)


@router.get("/memories")
def list_memories(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    store = MemoryStore(db=db, user_id=user.id)
    return {
        "memories": [
            {
                "id": m.id,
                "key": m.key,
                "value": m.value,
                "kind": m.kind,
                "updatedAt": m.updated_at.isoformat(),
            }
            for m in store.all()
        ]
    }


@router.post("/memories", status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    entry = MemoryStore(db=db, user_id=user.id).remember(payload.key, payload.value, payload.kind)
    return {"id": entry.id, "key": entry.key, "value": entry.value, "kind": entry.kind}


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_memory(
    memory_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    """Soft-delete: moves the memory to trash. Recoverable via restore below."""
    if not MemoryStore(db=db, user_id=user.id).forget(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")


@router.get("/memories/trash")
def list_trashed_memories(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    store = MemoryStore(db=db, user_id=user.id)
    return {
        "memories": [
            {
                "id": m.id,
                "key": m.key,
                "value": m.value,
                "kind": m.kind,
                "deletedAt": m.deleted_at.isoformat() if m.deleted_at else None,
            }
            for m in store.trash()
        ]
    }


@router.post("/memories/{memory_id}/restore")
def restore_memory(
    memory_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    entry = MemoryStore(db=db, user_id=user.id).restore(memory_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No trashed memory with that id.")
    return {"id": entry.id, "key": entry.key, "value": entry.value, "kind": entry.kind}


@router.delete(
    "/memories/{memory_id}/purge", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def purge_memory(
    memory_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    """Permanent delete of an already-trashed memory. Cannot be undone."""
    if not MemoryStore(db=db, user_id=user.id).purge(memory_id):
        raise HTTPException(status_code=404, detail="No trashed memory with that id.")


# --------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------


class TaskPayload(BaseModel):
    title: str = Field(default="Scheduled task", max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    scheduleKind: str = Field(default="once", pattern="^(once|interval)$")
    intervalSeconds: int = Field(default=0, ge=0, le=60 * 60 * 24 * 30)
    delaySeconds: int = Field(default=0, ge=0, le=60 * 60 * 24 * 30)
    provider: str | None = None


def _task_out(task: ScheduledTask) -> dict[str, object]:
    return {
        "id": task.id,
        "title": task.title,
        "prompt": task.prompt,
        "scheduleKind": task.schedule_kind,
        "intervalSeconds": task.interval_seconds,
        "nextRunAt": task.next_run_at.isoformat() if task.next_run_at else None,
        "lastRunAt": task.last_run_at.isoformat() if task.last_run_at else None,
        "lastStatus": task.last_status,
        "lastResult": task.last_result,
        "enabled": task.enabled,
        "provider": task.provider,
    }


@router.get("/tasks")
def list_tasks(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    rows = db.scalars(
        select(ScheduledTask)
        .where(ScheduledTask.user_id == user.id)
        .order_by(ScheduledTask.created_at.desc())
    )
    return {"tasks": [_task_out(t) for t in rows]}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, object]:
    row = _settings_row(db, user)
    task = ScheduledTask(
        user_id=user.id,
        title=payload.title,
        prompt=payload.prompt,
        schedule_kind=payload.scheduleKind,
        interval_seconds=payload.intervalSeconds,
        next_run_at=utcnow() + timedelta(seconds=payload.delaySeconds),
        provider=payload.provider or row.provider,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_out(task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_task(
    task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    task = db.get(ScheduledTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found.")
    db.delete(task)
    db.commit()


@router.post("/tasks/{task_id}/run")
def run_task(
    task_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict[str, object]:
    task = db.get(ScheduledTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found.")
    return run_task_now(task_id)


# --------------------------------------------------------------------------
# workspace files (uploads / downloads)
# --------------------------------------------------------------------------


@router.get("/workspace")
def workspace(user: User = Depends(get_current_user)) -> dict[str, object]:
    return workspace_info()


@router.get("/workspace/files")
def workspace_files(path: str = ".", user: User = Depends(get_current_user)) -> dict[str, object]:
    try:
        target = safe_path(path, must_exist=True)
    except (UnsafePath, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target.is_file():
        return {
            "path": relative(target),
            "type": "file",
            "content": target.read_text(encoding="utf-8", errors="replace")[
                : settings.max_file_bytes
            ],
        }
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        entries.append(
            {
                "path": relative(child),
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            }
        )
    return {"path": relative(target), "type": "dir", "entries": entries}


@router.post("/workspace/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile, path: str = "uploads", user: User = Depends(get_current_user)
) -> dict[str, object]:
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File is too large.")
    try:
        target = safe_path(str(Path(path) / (file.filename or "upload.bin")))
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": relative(target), "bytes": len(data)}


@router.get("/workspace/download")
def download_file(path: str, user: User = Depends(get_current_user)) -> FileResponse:
    try:
        target = safe_path(path, must_exist=True)
    except (UnsafePath, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory.")
    return FileResponse(str(target), filename=target.name)


@router.get("/workspace/root")
def workspace_root_path(user: User = Depends(get_current_user)) -> dict[str, str]:
    return {"root": str(workspace_root())}
