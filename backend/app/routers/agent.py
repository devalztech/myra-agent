"""Agent API: streaming runs, providers, memory, tasks, tools, workspace."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
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
from ..llm.prompts import title_from_message
from ..models import AgentEventRow, ChatMessage, Memory, ScheduledTask, User, UserSettings, utcnow
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


class AgentRunPayload(BaseModel):
    content: str = Field(min_length=1, max_length=16000)
    provider: str | None = None
    approved: bool = False


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/sessions/{session_id}/agent")
def run_agent(
    session_id: str,
    payload: AgentRunPayload,
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

    def stream() -> Iterator[str]:
        # A fresh DB session: the request-scoped one closes when the generator
        # starts streaming outside the dependency scope.
        local_db = SessionLocal()
        try:
            memory = MemoryStore(db=local_db, user_id=user_id)

            runner = AgentRunner(
                provider=get_provider(provider_id),
                memory=memory,
                budget=budget,
                approved=payload.approved,
            )
            yield _sse({"type": "session", **session_out})
            yield _sse({"type": "user_message", "message": user_message_out})

            events: list[dict[str, object]] = []
            final_text = ""
            for event in runner.run(prompt, history):
                data = event.dict()
                events.append(data)
                if event.type == "final":
                    final_text = str(event.data.get("text", ""))
                yield _sse(data)

            assistant = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=final_text or "(no reply)",
            )
            local_db.add(assistant)
            local_db.commit()
            local_db.refresh(assistant)
            for data in events:
                local_db.add(
                    AgentEventRow(
                        session_id=session_id,
                        message_id=assistant.id,
                        type=str(data.get("type", "event")),
                        payload=json.dumps(data)[:8000],
                    )
                )
            stored = local_db.get(ChatMessage, assistant.id)
            local_db.commit()
            yield _sse(
                {
                    "type": "done",
                    "message": {
                        "id": assistant.id,
                        "role": "assistant",
                        "content": assistant.content,
                        "createdAt": (stored or assistant).created_at.isoformat(),
                    },
                }
            )
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            logger.exception("Agent run failed")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
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
    if not MemoryStore(db=db, user_id=user.id).forget(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")


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
