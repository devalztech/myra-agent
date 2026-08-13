"""Scheduler for one-off and recurring agent tasks.

A single background daemon thread polls the ``scheduled_tasks`` table every
``MYRA_SCHEDULER_INTERVAL`` seconds, runs anything that is due through the
agent loop, records the outcome, and re-arms interval tasks. No extra process
or external cron is involved (host cron is blocked by the guardrails).
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import select

from .agent.guardrails import RunBudget
from .agent.loop import AgentRunner
from .agent.memory import MemoryStore
from .config import settings
from .database import SessionLocal
from .models import ScheduledTask, utcnow
from .providers import get_provider

logger = logging.getLogger("myra.scheduler")

_thread: threading.Thread | None = None
_stop = threading.Event()


def run_task_now(task_id: str) -> dict[str, object]:
    """Execute one task synchronously and persist the result."""
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None:
            return {"ok": False, "error": "Task not found."}
        memory = MemoryStore(db=db, user_id=task.user_id)
        runner = AgentRunner(
            provider=get_provider(task.provider),
            memory=memory,
            budget=RunBudget(),
        )
        final = ""
        status = "ok"
        try:
            for event in runner.run(task.prompt):
                if event.type == "final":
                    final = str(event.data.get("text", ""))
                elif event.type == "error":
                    status = "error"
                    final = str(event.data.get("message", ""))
        except Exception as exc:  # pragma: no cover - defensive
            status = "error"
            final = f"{type(exc).__name__}: {exc}"
            logger.exception("Scheduled task failed")

        task.last_run_at = utcnow()
        task.last_status = status
        task.last_result = final[:4000]
        if task.schedule_kind == "interval" and task.interval_seconds > 0:
            task.next_run_at = utcnow() + timedelta(seconds=task.interval_seconds)
        else:
            task.enabled = False
        db.commit()
        return {"ok": status == "ok", "result": final}
    finally:
        db.close()


def _due_task_ids() -> list[str]:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(ScheduledTask).where(
                ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= utcnow()
            )
        )
        return [row.id for row in rows]
    finally:
        db.close()


def _loop() -> None:
    interval = max(5, settings.scheduler_interval_seconds)
    logger.info("Scheduler started (every %ss)", interval)
    while not _stop.wait(interval):
        try:
            for task_id in _due_task_ids():
                logger.info("Running scheduled task %s", task_id)
                run_task_now(task_id)
        except Exception:  # pragma: no cover - keep the thread alive
            logger.exception("Scheduler tick failed")


def start_scheduler() -> None:
    global _thread
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled by configuration")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="myra-scheduler", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()
