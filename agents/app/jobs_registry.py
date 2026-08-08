"""Agents' job producer for the ``/internal/jobs/v1`` stream.

The ``agents`` service is a **v1 jobs producer**. It publishes two job types so the
``jobs`` service can view + control runtime-agent work:

- ``agent_turn`` — one per active turn (one user message → the streamed response),
  RUNNING → PAUSED (on a Principle-4 human-approval pause) → terminal. ``job_id``
  is the **turn id** (a uuid4 hex), the exact handle used for control.
- ``agent_schedule`` — one per persisted schedule (Phase 3): SCHEDULED while
  pending, a one-shot fires → RUNNING → COMPLETED, a recurring one stays SCHEDULED
  (each firing spawns its own ``agent_turn`` job); cancel → CANCELED. ``job_id`` is
  ``sched-<schedule id>`` (:data:`app.schedules.SCHEDULE_JOB_PREFIX`) so a schedule
  job is unambiguously distinguishable from a turn job in the single control
  callback.

An **idle** session emits **no** job — a job exists only for running/paused work
(turns) or scheduled work (schedules), per the producer contract.

Migrated onto the shared :class:`fm_runtime.JobProducer` (P10 — cross-cutting
producer plumbing lives once in ``fm_runtime``, not hand-rolled per service; this
also folds in the deferred quality review of the old bespoke broadcast hub).
``search/app/jobs_registry.py`` is the sibling reference wiring. This module
constructs the single :data:`job_producer` and supplies its two engine callbacks:

- ``enumerate_active_jobs`` — the authoritative active snapshot. Turns are
  in-memory (the producer's own latest-event cache already carries them), so the
  only durable out-of-band state to reload is **pending schedules** — read from
  Postgres so they survive a pod restart and are replayed to a (re)connecting
  ``jobs`` subscriber.
- ``apply_control`` — dispatches a control action onto a turn (via
  ``turn_runner``) or a schedule (cancel → the row flips to ``canceled`` and the
  DB-polling scheduler stops firing it), returning the ``JobEvent`` to re-publish.

The control contract needs **no request-scoped contextvars**: the route derives
its ``(status, applied)`` response straight from the ``JobEvent`` that
``dispatch_control`` returns (a ``JobEvent`` ⇒ applied; ``None`` ⇒ not applied) —
this is the "control-contract contextvar smell" cleanup the migration enables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fm_runtime import JobControlAction, JobEvent, JobProducer, JobStatus

from app.config import get_settings
from app.models import JOB_TYPE_AGENT_TURN
from app.schedules import (
    cancel_schedule,
    enumerate_active_schedule_events,
    schedule_event,
    schedule_id_from_job,
)

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    """Attribution carried alongside a run's job events.

    ``origin`` is always ``agent`` for a runtime-agent run; ``actor`` is the
    agents service client (``azp``) that acts downstream through MCP. Together
    they render "alice (via agent)"."""

    user: str
    origin: str
    actor: str


# turn_runner.control status word -> JobStatus for the re-published turn event.
_TURN_CONTROL_STATUS: dict[str, JobStatus] = {
    "running": JobStatus.RUNNING,
    "paused": JobStatus.PAUSED,
    "canceled": JobStatus.CANCELED,
    "cancelled": JobStatus.CANCELED,
    "completed": JobStatus.COMPLETED,
}


async def _enumerate_active_jobs() -> list[JobEvent]:
    """Active-job snapshot for the producer: pending schedules from Postgres.

    In-flight turns are already in the producer's in-memory latest cache (they
    publish on every transition), so only the durable schedule state needs
    reloading here — that is what makes a schedule survive a restart / reach a
    late subscriber."""
    return await enumerate_active_schedule_events()


async def _apply_turn_control(job_id: str, action: JobControlAction) -> JobEvent | None:
    """Apply pause/resume/cancel to a turn and return the event to re-publish.

    Returns ``None`` when there is no live turn to control (the route reports
    ``applied=False``) or when the turn's attribution is unknown to the hub."""
    from app.runner import turn_runner  # lazy: runner imports this module at top

    new_status_word, applied = await turn_runner.control(job_id, action.value)
    if not applied:
        return None
    prior = await job_producer.latest(job_id)
    if prior is None:
        # A live handle exists but the hub never saw an event (should not happen —
        # _execute publishes RUNNING first). Nothing to reconstruct attribution
        # from; the turn task will publish its own event at the next checkpoint.
        return None
    status = _TURN_CONTROL_STATUS.get(new_status_word, JobStatus.RUNNING)
    return JobEvent(
        job_id=job_id,
        type=prior.type,
        user=prior.user,
        origin=prior.origin,
        actor=prior.actor,
        status=status,
        progress=prior.progress,
        exit_status="ok" if status is JobStatus.CANCELED else None,
        meta={**prior.meta, "control": action.value},
    )


async def _apply_schedule_control(
    schedule_id: str, action: JobControlAction
) -> JobEvent | None:
    """Apply control to a schedule. Only ``cancel`` is meaningful (a pending
    schedule has no running work to pause/resume); cancel flips the row to
    ``canceled`` so the DB-polling scheduler stops firing it and returns a
    terminal CANCELED event to re-publish."""
    if action is not JobControlAction.CANCEL:
        # pause/resume of a not-yet-running schedule is a no-op.
        return None
    row = await cancel_schedule(schedule_id)
    if row is None:
        return None
    # Drop any in-memory captured token for this schedule (best-effort; lazy import
    # avoids a scheduler<->jobs_registry import cycle).
    try:
        from app.scheduler import agent_scheduler

        agent_scheduler.forget_token(schedule_id)
    except Exception:  # noqa: BLE001 — cleanup only
        pass
    return schedule_event(row, JobStatus.CANCELED, exit_status="ok")


async def _apply_control(job_id: str, action: JobControlAction) -> JobEvent | None:
    """The producer's ``apply_control`` engine callback: route to the turn or the
    schedule engine by job-id namespace."""
    schedule_id = schedule_id_from_job(job_id)
    if schedule_id is not None:
        return await _apply_schedule_control(schedule_id, action)
    return await _apply_turn_control(job_id, action)


#: The single producer instance for agents. ``enumerate_active_jobs`` reloads
#: pending schedules from Postgres; ``apply_control`` dispatches turn/schedule
#: control. Terminal retention bounds how long a finished job's last event lingers
#: for a late subscriber.
job_producer = JobProducer(
    enumerate_active_jobs=_enumerate_active_jobs,
    apply_control=_apply_control,
    terminal_retention_seconds=get_settings().job_terminal_retention_seconds,
    log=logger,
)


async def publish_job(
    *,
    job_id: str,
    ctx: JobContext,
    status: JobStatus,
    progress: float | None = None,
    exit_status: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Construct an ``agent_turn`` JobEvent (strict) for a turn and broadcast it."""
    event = JobEvent(
        job_id=str(job_id),
        type=JOB_TYPE_AGENT_TURN,
        user=ctx.user,
        origin=ctx.origin,
        actor=ctx.actor,
        status=status,
        progress=progress,
        exit_status=exit_status,
        meta=dict(meta or {}),
    )
    await job_producer.publish(event)


__all__ = ["JobContext", "job_producer", "publish_job"]
