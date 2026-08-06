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

from fm_runtime import ORIGIN_AGENT, JobEvent, JobStatus
from sqlalchemy import select, update

from app.cron import cron_next
from app.database import SessionLocal
from app.models import (
    JOB_TYPE_AGENT_SCHEDULE,
    SCHEDULE_CANCELED,
    SCHEDULE_COMPLETED,
    SCHEDULE_PAUSED,
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
    row: AgentSchedule,
    status: JobStatus,
    *,
    exit_status: str | None = None,
    extra_meta: dict | None = None,
) -> JobEvent:
    """Build the ``agent_schedule`` JobEvent for a schedule row + lifecycle status.

    ``meta`` carries the linkage/attribution the jobs UI needs: the owning
    session, whether it recurs, its next fire, and a short prompt preview.
    ``extra_meta`` folds in extra markers (e.g. ``needs_reauth`` on a P6 pause).
    Built strictly (real ``JobStatus`` enum) so a typo fails loud in-process."""
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
    if extra_meta:
        meta.update(extra_meta)
    return JobEvent(
        job_id=schedule_job_id(row.id),
        type=JOB_TYPE_AGENT_SCHEDULE,
        user=row.owner,
        origin=row.origin or ORIGIN_AGENT,
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
    """Active ``scheduled`` rows for one session (for the session-detail API
    contract + jobs-stream terminalization on delete). Excludes ``paused`` — those
    are dormant, not shown as active scheduled work."""
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


async def list_active_for_session(session_id: str) -> list[AgentSchedule]:
    """Schedules occupying a session's anti-abuse quota: ``scheduled`` OR ``paused``.

    A ``paused`` row is dormant but re-armable, so it still holds a slot — counting
    only ``scheduled`` here would let a create → let-the-token-lapse-into-paused →
    create cycle grow the ``agent_schedule`` table without bound (paused rows accrue
    every time the owner's short-lived token expires between interactions). Used for
    the per-session pending + recurring caps; distinct from
    :func:`list_scheduled_for_session` (active ``scheduled`` only, for display)."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentSchedule)
                .where(
                    AgentSchedule.session_id == session_id,
                    AgentSchedule.status.in_((SCHEDULE_SCHEDULED, SCHEDULE_PAUSED)),
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


async def claim_due_schedule(
    schedule_id: str, *, now: datetime, recurring: bool, next_run_at: datetime | None
) -> AgentSchedule | None:
    """Atomically CLAIM a due schedule for firing (multi-pod safe) — fix #1.

    A single UPDATE, guarded by ``status='scheduled' AND next_run_at IS NOT NULL AND
    next_run_at <= now``, commits the firing's state transition and returns the row's
    id. With several ``agents`` pods sharing one agents-db (the agents-canary binds
    the SAME prod agents-db secret), exactly ONE pod wins each firing: the loser's
    UPDATE re-evaluates the guard against the already-advanced row (Postgres
    READ COMMITTED re-checks the WHERE after the winner commits) and matches nothing.

    This is the point of no return — the caller only spawns the turn when a row is
    returned. It also:
      * fixes the one-shot ordering bug — the state transition is committed BEFORE
        the turn spawns, not after (so a crash between can't re-fire it); and
      * closes the cancel-vs-fire TOCTOU — a cancel flipping ``status`` away from
        ``scheduled`` between the poller's select and here makes the guard miss, so
        a just-cancelled schedule never fires.

    For a recurring schedule this advances ``next_run_at`` (status stays
    ``scheduled``; ``completed`` when the cron is exhausted, i.e. ``next_run_at`` is
    None); for a one-shot it completes the row. Returns the fresh (committed) row on
    a win, or ``None`` if this pod lost the race / the row was cancelled.
    """
    new_status = SCHEDULE_SCHEDULED if (recurring and next_run_at is not None) else SCHEDULE_COMPLETED
    new_next = next_run_at if new_status == SCHEDULE_SCHEDULED else None
    stmt = (
        update(AgentSchedule)
        .where(
            AgentSchedule.id == schedule_id,
            AgentSchedule.status == SCHEDULE_SCHEDULED,
            AgentSchedule.next_run_at.isnot(None),
            AgentSchedule.next_run_at <= now,
        )
        .values(status=new_status, last_run_at=now, next_run_at=new_next)
        .returning(AgentSchedule.id)
    )
    async with SessionLocal() as db:
        won = (await db.execute(stmt)).scalar_one_or_none() is not None
        await db.commit()
    if not won:
        return None
    # Re-read the committed row (scalar RETURNING keeps the claim UPDATE free of
    # ORM-entity-from-RETURNING subtleties; the follow-up read is on a 10s poll — perf
    # is irrelevant, correctness is not).
    return await get_schedule(schedule_id)


async def pause_for_reauth(schedule_id: str) -> AgentSchedule | None:
    """Pause a pending schedule because the owner's authorization can no longer be
    proven (fix #3, P6). Flips ``scheduled -> paused`` atomically so the DB-polling
    select (which only takes ``scheduled`` rows) stops firing it, and clears
    ``next_run_at``. Returns the row on success, else None (already terminal / lost
    a race). Re-armed only by :func:`rearm_paused` when the owner returns."""
    stmt = (
        update(AgentSchedule)
        .where(
            AgentSchedule.id == schedule_id,
            AgentSchedule.status == SCHEDULE_SCHEDULED,
        )
        .values(status=SCHEDULE_PAUSED, next_run_at=None)
        .returning(AgentSchedule.id)
    )
    async with SessionLocal() as db:
        paused = (await db.execute(stmt)).scalar_one_or_none() is not None
        await db.commit()
    if not paused:
        return None
    return await get_schedule(schedule_id)


async def rearm_paused(session_id: str, now: datetime) -> list[AgentSchedule]:
    """Re-arm a session's re-auth-paused schedules (fix #3). Called when the owner
    posts a fresh message — which they can only do while they still hold
    ``agents-access`` — so re-arming is implicitly gated by CURRENT authorization,
    tying schedule liveness back to the owner's live grant.

    Recomputes ``next_run_at`` from cron for a recurring schedule (or completes it if
    the cron is exhausted); a one-shot that missed its window while paused fires ASAP
    (``next_run_at = now``). Returns the rows flipped back to ``scheduled`` (the
    caller re-stores their captured token and republishes them)."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentSchedule).where(
                    AgentSchedule.session_id == session_id,
                    AgentSchedule.status == SCHEDULE_PAUSED,
                )
            )
        ).scalars().all()
        rearmed: list[AgentSchedule] = []
        for row in rows:
            if is_recurring(row.spec):
                nxt = cron_next(row.spec["cron"], now)
                if nxt is None:
                    row.status = SCHEDULE_COMPLETED
                    row.next_run_at = None
                    continue
                row.next_run_at = nxt
            else:
                row.next_run_at = now
            row.status = SCHEDULE_SCHEDULED
            rearmed.append(row)
        await db.commit()
        for row in rearmed:
            db.expunge(row)
        return rearmed


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
    "cancel_schedule",
    "claim_due_schedule",
    "create_schedule",
    "enumerate_active_schedule_events",
    "get_schedule",
    "is_recurring",
    "list_active_for_session",
    "list_due_schedules",
    "list_scheduled",
    "list_scheduled_for_session",
    "pause_for_reauth",
    "rearm_paused",
    "schedule_event",
    "schedule_id_from_job",
    "schedule_job_id",
]
