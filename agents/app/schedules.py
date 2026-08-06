"""Persistence + job-event shaping for **agent schedules** (Phase 3).

A schedule is future/recurring runtime-agent work a running turn asked for via the
internal ``schedule_agent_job`` tool — it survives the tab (and the process,
because it lives in Postgres, reloaded on boot by the scheduler) and surfaces in
the ``jobs`` service as an ``agent_schedule`` job.

This module is the **data layer**: it owns the ``agent_schedule`` table CRUD and
turns a schedule row into a :class:`~fm_runtime.JobEvent` for the jobs producer.
It deliberately imports **nothing** from ``app.scheduler`` / ``app.jobs_registry``
so it can be pulled in by both without a cycle (the producer's
``enumerate_active_jobs`` and control callbacks read schedules through here).

Job-id namespace: a schedule's job id on the ``/internal/jobs`` stream is
``sched-<row id>`` (:data:`SCHEDULE_JOB_PREFIX`), so the single ``apply_control``
callback can tell a schedule job (``sched-…``) apart from a turn job (a bare
uuid) unambiguously — mirrors search's ``sem-<id>`` synthetic ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fm_runtime import JobEvent, JobStatus
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    JOB_TYPE_AGENT_SCHEDULE,
    SCHEDULE_CANCELED,
    SCHEDULE_COMPLETED,
    SCHEDULE_SCHEDULED,
    AgentSchedule,
)

# Prefix that namespaces a schedule's job id on the jobs stream (vs a turn's bare
# uuid). Kept here next to the row/JobEvent shaping that produces + consumes it.
SCHEDULE_JOB_PREFIX = "sched-"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def schedule_job_id(schedule_id: str) -> str:
    """The jobs-stream job id for a schedule row id."""
    return f"{SCHEDULE_JOB_PREFIX}{schedule_id}"


def schedule_id_from_job(job_id: str) -> str | None:
    """The schedule row id for a ``sched-…`` job id, or None if not a schedule job."""
    if job_id.startswith(SCHEDULE_JOB_PREFIX):
        return job_id[len(SCHEDULE_JOB_PREFIX) :]
    return None


def is_recurring(spec: dict | None) -> bool:
    """A schedule is recurring iff its spec carries a ``cron`` expression."""
    return bool(spec) and "cron" in spec


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def schedule_event(
    row: AgentSchedule, status: JobStatus, *, exit_status: str | None = None
) -> JobEvent:
    """Build the ``agent_schedule`` JobEvent for a schedule row + lifecycle status.

    ``meta`` carries the linkage/attribution the jobs UI needs: the owning
    session, whether it recurs, its next fire, and a short prompt preview. Built
    strictly (real ``JobStatus`` enum) so a typo fails loud in-process."""
    spec = row.spec or {}
    recurring = is_recurring(spec)
    meta = {
        "session_id": row.session_id,
        "kind": "recurring" if recurring else "once",
        "next_run_at": _iso(row.next_run_at),
        "last_run_at": _iso(row.last_run_at),
        "prompt": (row.prompt or "")[:200],
    }
    if recurring:
        meta["cron"] = spec.get("cron")
    elif "at" in spec:
        meta["at"] = spec.get("at")
    return JobEvent(
        job_id=schedule_job_id(row.id),
        type=JOB_TYPE_AGENT_SCHEDULE,
        user=row.owner,
        origin=row.origin or "agent",
        actor=row.actor or "",
        status=status,
        exit_status=exit_status,
        meta=meta,
    )


# --- CRUD -------------------------------------------------------------------


async def create_schedule(
    *,
    session_id: str,
    owner: str,
    origin: str,
    actor: str,
    spec: dict,
    prompt: str,
    next_run_at: datetime,
) -> AgentSchedule:
    """Persist a new scheduled schedule row and return it."""
    row = AgentSchedule(
        id=uuid.uuid4().hex,
        session_id=session_id,
        owner=owner,
        origin=origin,
        actor=actor,
        spec=dict(spec or {}),
        prompt=prompt,
        status=SCHEDULE_SCHEDULED,
        next_run_at=next_run_at,
    )
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
        # Detach a plain copy so callers can read attributes after the session
        # closes without a lazy-load on an expired instance.
        db.expunge(row)
    return row


async def get_schedule(schedule_id: str) -> AgentSchedule | None:
    async with SessionLocal() as db:
        row = await db.get(AgentSchedule, schedule_id)
        if row is not None:
            db.expunge(row)
        return row


async def list_due_schedules(now: datetime) -> list[AgentSchedule]:
    """Scheduled rows whose ``next_run_at`` has arrived (single-replica poller)."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentSchedule)
                .where(
                    AgentSchedule.status == SCHEDULE_SCHEDULED,
                    AgentSchedule.next_run_at.isnot(None),
                    AgentSchedule.next_run_at <= now,
                )
                .order_by(AgentSchedule.next_run_at.asc())
            )
        ).scalars().all()
        for row in rows:
            db.expunge(row)
        return list(rows)


async def list_scheduled() -> list[AgentSchedule]:
    """All still-pending schedules — the authoritative active snapshot for the
    jobs producer's ``enumerate_active_jobs`` (reloaded on every subscribe/boot)."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentSchedule)
                .where(AgentSchedule.status == SCHEDULE_SCHEDULED)
                .order_by(AgentSchedule.next_run_at.asc())
            )
        ).scalars().all()
        for row in rows:
            db.expunge(row)
        return list(rows)


async def list_scheduled_for_session(session_id: str) -> list[AgentSchedule]:
    """Pending schedules for one session (for the session-detail API contract)."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentSchedule)
                .where(
                    AgentSchedule.session_id == session_id,
                    AgentSchedule.status == SCHEDULE_SCHEDULED,
                )
                .order_by(AgentSchedule.created_at.asc())
            )
        ).scalars().all()
        for row in rows:
            db.expunge(row)
        return list(rows)


async def enumerate_active_schedule_events() -> list[JobEvent]:
    """The jobs producer's active-schedule snapshot: one SCHEDULED JobEvent per
    pending schedule. Reads Postgres so schedules survive a pod restart and are
    replayed to a (re)connecting ``jobs`` subscriber."""
    return [schedule_event(row, JobStatus.SCHEDULED) for row in await list_scheduled()]


async def mark_fired_oneshot(schedule_id: str, *, fired_at: datetime) -> None:
    """One-shot fired: complete the row and stamp ``last_run_at`` so it is never
    re-selected."""
    async with SessionLocal() as db:
        row = await db.get(AgentSchedule, schedule_id)
        if row is not None and row.status == SCHEDULE_SCHEDULED:
            row.status = SCHEDULE_COMPLETED
            row.last_run_at = fired_at
            row.next_run_at = None
            await db.commit()


async def advance_recurring(
    schedule_id: str, *, fired_at: datetime, next_run_at: datetime | None
) -> None:
    """Recurring fired: stamp ``last_run_at`` and set the next fire (or complete
    the row if the cron has no further occurrence)."""
    async with SessionLocal() as db:
        row = await db.get(AgentSchedule, schedule_id)
        if row is None or row.status != SCHEDULE_SCHEDULED:
            return
        row.last_run_at = fired_at
        if next_run_at is None:
            row.status = SCHEDULE_COMPLETED
            row.next_run_at = None
        else:
            row.next_run_at = next_run_at
        await db.commit()


async def cancel_schedule(schedule_id: str) -> AgentSchedule | None:
    """Cancel a pending schedule (stops future firings — the poller only selects
    ``scheduled`` rows). Returns the updated row, or None if unknown / not pending."""
    async with SessionLocal() as db:
        row = await db.get(AgentSchedule, schedule_id)
        if row is None or row.status != SCHEDULE_SCHEDULED:
            return None
        row.status = SCHEDULE_CANCELED
        row.next_run_at = None
        await db.commit()
        await db.refresh(row)
        db.expunge(row)
        return row


__all__ = [
    "SCHEDULE_JOB_PREFIX",
    "advance_recurring",
    "cancel_schedule",
    "create_schedule",
    "enumerate_active_schedule_events",
    "get_schedule",
    "is_recurring",
    "list_due_schedules",
    "list_scheduled",
    "list_scheduled_for_session",
    "mark_fired_oneshot",
    "schedule_event",
    "schedule_id_from_job",
    "schedule_job_id",
]
