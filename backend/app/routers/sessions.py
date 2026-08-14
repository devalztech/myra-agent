"""Chat session CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, get_owned_session
from ..models import ChatSession, User
from ..schemas import (
    CreateSessionPayload,
    MessageOut,
    RenameSessionPayload,
    SessionOut,
    SessionSummary,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def to_summary(session: ChatSession) -> SessionSummary:
    return SessionSummary(id=session.id, title=session.title, updatedAt=session.updated_at)


def to_detail(session: ChatSession) -> SessionOut:
    return SessionOut(
        id=session.id,
        title=session.title,
        updatedAt=session.updated_at,
        messages=[
            MessageOut(id=m.id, role=m.role, content=m.content, createdAt=m.created_at)
            for m in session.messages
        ],
    )


@router.get("", response_model=list[SessionSummary])
def list_sessions(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[SessionSummary]:
    rows = db.scalars(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    return [to_summary(s) for s in rows]


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: CreateSessionPayload | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionOut:
    title = (payload.title if payload and payload.title else None) or "New session"
    session = ChatSession(user_id=user.id, title=title)
    db.add(session)
    db.commit()
    db.refresh(session)
    return to_detail(session)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionOut:
    return to_detail(get_owned_session(session_id, db, user))


@router.patch("/{session_id}", response_model=SessionSummary)
def rename_session(
    session_id: str,
    payload: RenameSessionPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionSummary:
    session = get_owned_session(session_id, db, user)
    session.title = payload.title.strip()
    db.commit()
    db.refresh(session)
    return to_summary(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    session = get_owned_session(session_id, db, user)
    db.delete(session)
    db.commit()
    # Best-effort: release this session's persistent browser (if the agent
    # ever opened one) instead of leaving a chromium process idling until
    # the manager's own idle sweeper eventually reaps it.
    try:
        from ..services.browser import close_session

        close_session(session_id)
    except Exception:
        pass
