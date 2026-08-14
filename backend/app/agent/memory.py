"""Long-term memory: user preferences and project conventions.

Memory is stored per user in the database and injected into prompts as a small
digest (a handful of one-line rules), never as a raw dump. Retrieval scores
entries by keyword overlap with the current request so a long memory list
still costs only a few hundred tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Memory, utcnow

# How long a forgotten memory sits recoverable before purge_expired() can
# remove it for good. Purge is never automatic/scheduled on its own — call
# sites decide when to run it — this constant just defines "expired".
TRASH_TTL_DAYS = 30

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "for", "with", "on", "is",
    "it", "this", "that", "my", "me", "you", "please", "can", "should", "be",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_.-]+", (text or "").lower()) if w not in STOPWORDS}


@dataclass
class MemoryStore:
    db: Session
    user_id: str

    # -- writes ---------------------------------------------------------
    def remember(self, key: str, value: str, kind: str = "preference") -> Memory:
        key = (key or "").strip()[:160] or "note"
        existing = self.db.scalar(
            select(Memory).where(Memory.user_id == self.user_id, Memory.key == key)
        )
        if existing:
            existing.value = value
            existing.kind = kind
            existing.updated_at = utcnow()
            existing.deleted_at = None  # re-remembering un-forgets it too
            entry = existing
        else:
            entry = Memory(user_id=self.user_id, key=key, value=value, kind=kind)
            self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def forget(self, memory_id: str) -> bool:
        """Soft-delete: the row stays, just hidden from all()/search()/digest().

        Recoverable via restore() until purge_expired() (or restore_all's
        counterpart, a hard `purge`) actually removes it.
        """
        entry = self.db.get(Memory, memory_id)
        if entry is None or entry.user_id != self.user_id or entry.deleted_at is not None:
            return False
        entry.deleted_at = utcnow()
        self.db.commit()
        return True

    def restore(self, memory_id: str) -> Memory | None:
        entry = self.db.get(Memory, memory_id)
        if entry is None or entry.user_id != self.user_id or entry.deleted_at is None:
            return None
        entry.deleted_at = None
        entry.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def purge(self, memory_id: str) -> bool:
        """Hard delete — permanently removes an already-forgotten memory."""
        entry = self.db.get(Memory, memory_id)
        if entry is None or entry.user_id != self.user_id or entry.deleted_at is None:
            return False
        self.db.delete(entry)
        self.db.commit()
        return True

    def purge_expired(self, ttl_days: int = TRASH_TTL_DAYS) -> int:
        """Hard-delete trashed memories older than ttl_days. Returns count removed."""
        cutoff = utcnow() - timedelta(days=ttl_days)
        stale = list(
            self.db.scalars(
                select(Memory).where(
                    Memory.user_id == self.user_id,
                    Memory.deleted_at.is_not(None),
                    Memory.deleted_at < cutoff,
                )
            )
        )
        for entry in stale:
            self.db.delete(entry)
        if stale:
            self.db.commit()
        return len(stale)

    # -- reads ----------------------------------------------------------
    def all(self) -> list[Memory]:
        return list(
            self.db.scalars(
                select(Memory)
                .where(Memory.user_id == self.user_id, Memory.deleted_at.is_(None))
                .order_by(Memory.updated_at.desc())
            )
        )

    def trash(self) -> list[Memory]:
        """Forgotten-but-not-yet-purged memories, most recently deleted first."""
        return list(
            self.db.scalars(
                select(Memory)
                .where(Memory.user_id == self.user_id, Memory.deleted_at.is_not(None))
                .order_by(Memory.deleted_at.desc())
            )
        )

    def search(self, query: str = "", limit: int = 12) -> list[dict[str, str]]:
        entries = self.all()
        if query:
            wanted = _tokens(query)
            scored = sorted(
                entries,
                key=lambda m: len(wanted & _tokens(f"{m.key} {m.value}")),
                reverse=True,
            )
            entries = [m for m in scored if wanted & _tokens(f"{m.key} {m.value}")] or entries
        return [
            {"id": m.id, "key": m.key, "value": m.value, "kind": m.kind}
            for m in entries[:limit]
        ]

    def digest(self, query: str = "", limit: int = 8) -> str:
        """Compact prompt block — the only memory the model ever sees upfront.

        A pure top-N-by-relevance cut silently drops anything outside the
        top `limit` forever once a user has more than `limit` memories,
        unless a later message happens to reuse the same words — which
        reads as Myra "forgetting" things it was told days ago. This blends
        two views instead of one: keyword-relevant to the current message,
        topped up with whatever's most recently touched overall, so a
        durable fact keeps surfacing even when the current message doesn't
        share vocabulary with how it was originally phrased.
        """
        entries = self.all()
        if not entries:
            return ""

        rows = self.search(query, limit=limit) if query else []
        seen = {r["id"] for r in rows}
        for m in entries:
            if len(rows) >= limit:
                break
            if m.id in seen:
                continue
            rows.append({"id": m.id, "key": m.key, "value": m.value, "kind": m.kind})
            seen.add(m.id)

        if not rows:
            return ""
        lines = [f"- ({r['kind']}) {r['key']}: {r['value']}" for r in rows]
        return "Known user preferences and project conventions:\n" + "\n".join(lines)
