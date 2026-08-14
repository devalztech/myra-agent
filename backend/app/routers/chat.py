"""Chat endpoints: buffered JSON reply and SSE token streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, get_db
from ..deps import get_current_user, get_owned_session
from ..llmutil import LLMUnavailable, title_from_message
from ..models import ChatMessage, ChatSession, User, utcnow
from ..providers import get_provider
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
    provider = get_provider()
    return ModelStatus(
        backend=provider.kind,
        model=provider.model,
        loaded=provider.available,
        contextSize=0,
        ramGb=0,
        tier=provider.kind,
        status="ready" if provider.available else "error",
        detail=provider.detail,
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

    try:
        provider = get_provider()
        reply = provider.complete([*history, {"role": "user", "content": payload.content}]).strip()
    except LLMUnavailable as exc:
        logger.error("Provider unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Inference failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The model failed to generate a response.",
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
    request: Request,
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

    async def generate() -> AsyncIterator[str]:
        yield _sse("session", {"id": session_id, "title": session_title})
        yield _sse("user_message", user_message_payload)

        provider = get_provider()
        pieces: list[str] = []
        stopped = False
        try:
            for index, token in enumerate(
                provider.stream([*history, {"role": "user", "content": payload.content}])
            ):
                pieces.append(token)
                yield _sse("token", {"token": token})
                # Check every few tokens rather than every single one — the
                # user tapped Stop / closed the tab, so no one is listening
                # for further tokens and local inference can be abandoned.
                if index % 8 == 0 and await request.is_disconnected():
                    stopped = True
                    break
        except LLMUnavailable as exc:
            yield _sse("error", {"message": str(exc)})
            return
        except Exception:  # pragma: no cover - defensive
            logger.exception("Streaming inference failed")
            yield _sse("error", {"message": "The model failed to generate a response."})
            return

        reply = "".join(pieces).strip() or ("(stopped)" if stopped else "(empty response)")
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
        if stopped:
            return  # nothing left listening — skip the final SSE writes
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
