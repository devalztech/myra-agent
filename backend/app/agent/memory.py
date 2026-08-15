"""Long-term memory: user preferences and project conventions.

Memory is stored per user in the database and injected into prompts as a small
digest (a handful of one-line rules), never as a raw dump. Retrieval scores
entries by keyword overlap with the current request so a long memory list
still costs only a few hundred tokens.
"""

from __future__ import annotations

import re
import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Memory, utcnow

# How long a forgotten memory sits recoverable before purge_expired() can
# remove it for good. Purge is never automatic/scheduled on its own — call
# sites decide when to run it — this constant just defines "expired".
TRASH_TTL_DAYS = 30
LOG_CAP = 200  # keep at most this many auto-log entries per user (prune older)

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
    project: str | None = None

    # -- working / auto memory ----------------------------------------
    TASK_STATE_KEY = "task.current"

    def log_step(self, action: str, detail: str = "", *, result: str = "") -> Memory:
        """Append a step to the persistent action log (kind='log').

        Auto-called by the agent loop after every tool call so the full trail
        of what myra did is saved — this is what lets it "know what it's doing"
        and pick up cleanly after a reconnect or new session. Kept compact and
        rotated so the log doesn't blow up the digest.
        """
        stamp = time.strftime("%Y-%m-%d %H:%M")
        line = f"[{stamp}] {action}"
        if detail:
            line += f" {detail}"
        if result:
            line += f" -> {result[:200]}"
        return self.remember(f"log.{int(time.time())}.{uuid.uuid4().hex[:6]}", line, kind="log")

    def set_task_state(self, summary: str, *, goal: str = "", progress: str = "", next_step: str = "") -> Memory:
        """Record what myra is currently working on and where it is.

        A single durable 'current task' memory that the model sees in its
        digest, so it always knows what it's doing, what's done, and what's
        next — across sessions and reconnects.
        """
        value = json.dumps(
            {"goal": goal, "progress": progress, "next": next_step, "summary": summary},
            ensure_ascii=False,
        )
        return self.remember(self.TASK_STATE_KEY, value, kind="task_state")

    def get_task_state(self) -> dict[str, str]:
        """Return the current task state dict (empty if none)."""
        entry = self.db.scalar(
            select(Memory).where(Memory.user_id == self.user_id, Memory.key == self.TASK_STATE_KEY)
        )
        if entry is None or entry.deleted_at is not None:
            return {}
        try:
            data = json.loads(entry.value)
            return data if isinstance(data, dict) else {"summary": str(data)}
        except (json.JSONDecodeError, TypeError):
            return {"summary": entry.value}

    def clear_task_state(self) -> None:
        entry = self.db.scalar(
            select(Memory).where(Memory.user_id == self.user_id, Memory.key == self.TASK_STATE_KEY)
        )
        if entry is not None:
            entry.deleted_at = utcnow()
            self.db.commit()

    def recent_logs(self, limit: int = 6) -> list[str]:
        """Most recent auto-log entries, newest first."""
        rows = self.all()
        logs = [m.value for m in rows if m.kind == "log"]
        return logs[:limit]

    def forget_logs(self) -> int:
        """Soft-delete auto logs (keep durable prefs/conventions)."""
        logs = [m for m in self.all() if m.kind == "log"]
        for m in logs:
            m.deleted_at = utcnow()
        if logs:
            self.db.commit()
        return len(logs)

    def prune_logs(self, cap: int = LOG_CAP) -> int:
        """Trim old auto-logs so memory never balloons.

        Keeps the newest ``cap`` log entries and hard-deletes the rest (logs are
        transient working memory — the durable facts live in preference/convention
        memories). Called after each run so the DB stays small and the digest fast.
        """
        logs = [m for m in self.all() if m.kind == "log"]
        logs.sort(key=lambda m: m.updated_at or m.created_at, reverse=True)
        stale = logs[cap:]
        for m in stale:
            self.db.delete(m)
        if stale:
            self.db.commit()
        return len(stale)

    def learn(self, key: str, value: str, *, kind: str = "convention") -> Memory:
        """Persist a durable lesson distilled from a run (auto-reflection).

        Same dedup as remember() but defaults to 'convention' and is intended
        for auto-extracted facts, not user-typed ones. Upserts on key so the
        same lesson doesn't duplicate over many runs.
        """
        return self.remember(key=key, value=value, kind=kind)

    # -- writes ---------------------------------------------------------
    def remember(self, key: str, value: str, kind: str = "preference") -> Memory:
        key = (key or "").strip()[:160] or "note"
        existing = self.db.scalar(
            select(Memory).where(
                Memory.user_id == self.user_id,
                Memory.project == self.project,
                Memory.key == key,
            )
        )
        if existing:
            existing.value = value
            existing.kind = kind
            existing.updated_at = utcnow()
            existing.deleted_at = None  # re-remembering un-forgets it too
            entry = existing
        else:
            entry = Memory(
                user_id=self.user_id, project=self.project, key=key, value=value, kind=kind
            )
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
        # Project scope includes project-specific memories plus global memories.
        # Project-specific entries win during retrieval, while global preferences
        # remain available to every project.
        filters = [Memory.user_id == self.user_id, Memory.deleted_at.is_(None)]
        if self.project:
            filters.append((Memory.project == self.project) | (Memory.project.is_(None)))
        return list(
            self.db.scalars(
                select(Memory)
                .where(*filters)
                .order_by(Memory.updated_at.desc())
            )
        )

    def trash(self) -> list[Memory]:
        """Forgotten-but-not-yet-purged memories, most recently deleted first."""
        filters = [Memory.user_id == self.user_id, Memory.deleted_at.is_not(None)]
        if self.project:
            filters.append(Memory.project == self.project)
        return list(
            self.db.scalars(
                select(Memory)
                .where(*filters)
                .order_by(Memory.deleted_at.desc())
            )
        )

    def search(self, query: str = "", limit: int = 12) -> list[dict[str, str]]:
        entries = self.all()
        now = time.time()
        if query:
            wanted = _tokens(query)
            scored = sorted(
                entries,
                key=lambda m: (
                    len(wanted & _tokens(f"{m.key} {m.value}")),
                    # fresher entries win ties (recency-aware retrieval)
                    -abs((m.updated_at or m.created_at).timestamp() - now),
                ),
                reverse=True,
            )
            entries = [m for m in scored if wanted & _tokens(f"{m.key} {m.value}")] or entries
        return [
            {
                "id": m.id,
                "key": m.key,
                "value": m.value,
                "kind": m.kind,
                "updatedAt": (m.updated_at or m.created_at).isoformat(),
            }
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

        # Always surface the current task state first — this is the "what am I
        # doing" anchor so myra knows where it is even after a reconnect.
        task = self.get_task_state()
        blocks: list[str] = []
        if task:
            summary = task.get("summary") or task.get("goal") or ""
            state_lines = [f"  - Goal: {summary}"]
            if task.get("progress"):
                state_lines.append(f"  - Progress: {task['progress']}")
            if task.get("next"):
                state_lines.append(f"  - Next: {task['next']}")
            blocks.append("Current task state:\n" + "\n".join(state_lines))

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
            if not blocks:
                return ""
        # Only durable preferences/conventions belong in the "known" block;
        # logs and task_state are surfaced separately above to avoid noise.
        durable = [r for r in rows if r["kind"] not in ("log", "task_state")]
        lines = [f"- ({r['kind']}) {r['key']}: {r['value']}" for r in durable]
        if lines:
            blocks.append("Known preferences & conventions:\n" + "\n".join(lines))

        # Include a short tail of what myra has been doing recently.
        logs = self.recent_logs(limit=4)
        if logs:
            blocks.append("Recent actions:\n" + "\n".join(f"  - {log}" for log in reversed(logs)))

        return "\n\n".join(block for block in blocks if block)
