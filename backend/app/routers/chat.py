"""Chat endpoints: buffered JSON reply and SSE token streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, get_db
from ..deps import get_current_user, get_owned_session
from ..llm.engine import LLMUnavailable, build_messages, get_engine
from ..llm.prompts import title_from_message
from ..models import ChatMessage, ChatSession, User, utcnow
from ..schemas import ChatPayload, ChatResponse, MessageOut, ModelStatus
from .sessions import to_summary

logger = logging.getLogger("myra.chat")

router = APIRouter(tags=["chat"])


def _history(session: ChatSession) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in session.messages]


def _record_user_message(
    db: Session, session: ChatSession, content: str
) -> tuple[ChatMessage, list[dict[str, str]]]:
    history = _history(session)
    message = ChatMessage(session_id=session.id, role="user", content=content)
    db.add(message)
    if not session.messages:
        session.title = title_from_message(content)
    session.updated_at = utcnow()
    db.commit()
    db.refresh(message)
    db.refresh(session)
    return message, history


@router.get("/model", response_model=ModelStatus)
def model_status() -> ModelStatus:
    engine = get_engine()
    return ModelStatus(
        backend=engine.backend,
        model=engine.model_name,
        loaded=engine.loaded,
        contextSize=engine.context_size,
        ramGb=engine.ram_gb,
        tier=engine.tier.name,
        status=engine.status,
        detail=engine.detail,
        threads=settings.threads,
    )


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(
    session_id: str,
    payload: ChatPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatResponse:
    session = get_owned_session(session_id, db, user)
    user_message, history = _record_user_message(db, session, payload.content)

    engine = get_engine()
    try:
        reply = engine.complete(build_messages(history, payload.content)).strip()
    except LLMUnavailable as exc:
        logger.error("Local model unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Inference failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Local model failed to generate a response.",
        ) from exc

    assistant = ChatMessage(
        session_id=session.id, role="assistant", content=reply or "(empty response)"
    )
    db.add(assistant)
    session.updated_at = utcnow()
    db.commit()
    db.refresh(assistant)
    db.refresh(session)

    return ChatResponse(
        session=to_summary(session),
        userMessage=MessageOut(
            id=user_message.id,
            role=user_message.role,
            content=user_message.content,
            createdAt=user_message.created_at,
        ),
        assistantMessage=MessageOut(
            id=assistant.id,
            role=assistant.role,
            content=assistant.content,
            createdAt=assistant.created_at,
        ),
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/sessions/{session_id}/chat/stream")
def chat_stream(
    session_id: str,
    payload: ChatPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    session = get_owned_session(session_id, db, user)
    user_message, history = _record_user_message(db, session, payload.content)
    session_title = session.title
    user_message_payload = {
        "id": user_message.id,
        "role": "user",
        "content": user_message.content,
        "createdAt": user_message.created_at.isoformat(),
    }

    def generate() -> Iterator[str]:
        yield _sse("session", {"id": session_id, "title": session_title})
        yield _sse("user_message", user_message_payload)

        engine = get_engine()
        if not engine.loaded:
            # First message after a cold boot can sit for a while behind a
            # model download/load. Tell the UI instead of looking frozen.
            yield _sse(
                "status",
                {
                    "state": engine.status,
                    "message": engine.detail or "Preparing the local model…",
                },
            )
        pieces: list[str] = []
        try:
            for token in engine.stream(build_messages(history, payload.content)):
                pieces.append(token)
                yield _sse("token", {"token": token})
        except LLMUnavailable as exc:
            yield _sse("error", {"message": str(exc)})
            return
        except Exception:  # pragma: no cover - defensive
            logger.exception("Streaming inference failed")
            yield _sse("error", {"message": "Local model failed to generate a response."})
            return

        reply = "".join(pieces).strip() or "(empty response)"
        # Fresh session: the request-scoped one may already be closed mid-stream.
        with SessionLocal() as write_db:
            assistant = ChatMessage(session_id=session_id, role="assistant", content=reply)
            write_db.add(assistant)
            stored = write_db.get(ChatSession, session_id)
            if stored is not None:
                stored.updated_at = utcnow()
            write_db.commit()
            write_db.refresh(assistant)
            payload_out = {
                "id": assistant.id,
                "role": "assistant",
                "content": assistant.content,
                "createdAt": assistant.created_at.isoformat(),
            }
        yield _sse("assistant_message", payload_out)
        yield _sse("done", {"ok": True})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
