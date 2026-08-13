"""Long-term memory: user preferences and project conventions.

Memory is stored per user in the database and injected into prompts as a small
digest (a handful of one-line rules), never as a raw dump. Retrieval scores
entries by keyword overlap with the current request so a long memory list
still costs only a few hundred tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Memory, utcnow

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
            entry = existing
        else:
            entry = Memory(user_id=self.user_id, key=key, value=value, kind=kind)
            self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def forget(self, memory_id: str) -> bool:
        entry = self.db.get(Memory, memory_id)
        if entry is None or entry.user_id != self.user_id:
            return False
        self.db.delete(entry)
        self.db.commit()
        return True

    # -- reads ----------------------------------------------------------
    def all(self) -> list[Memory]:
        return list(
            self.db.scalars(
                select(Memory)
                .where(Memory.user_id == self.user_id)
                .order_by(Memory.updated_at.desc())
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
        """Compact prompt block — the only memory the model ever sees upfront."""
        rows = self.search(query, limit=limit)
        if not rows:
            return ""
        lines = [f"- ({r['kind']}) {r['key']}: {r['value']}" for r in rows]
        return "Known user preferences and project conventions:\n" + "\n".join(lines)
