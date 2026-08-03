"""Job-row upsert from a producer's lifecycle events.

The `jobs` service is an OBSERVER: it never runs work, it mirrors the state a
producing app publishes on its ``/internal/jobs/v1/stream``. This module maps a
:class:`fm_runtime.JobEvent` onto a :class:`app.models.Job` row.

Idempotency + replay-safety (the load-bearing property): a producer stream
buffers per-job history, so a subscriber that (re)connects re-reads a job's
whole lifecycle, and events can be redelivered. Every app+external_job_id is
written by exactly one stream reader (sequential per producer), so there is no
concurrent writer for a given key — but there IS redelivery and possible
out-of-order arrival. Two guards keep the state monotonic:

1. Terminal is absorbing (the load-bearing invariant: a job never leaves a
   terminal state). Once the stored row is completed/failed/canceled, ANY
   non-terminal event is dropped outright, regardless of ts — this defeats a
   late or equal-ts replayed ``running`` event that the ts guard alone would
   admit.
2. Timestamp ordering. Among non-terminal transitions, an event strictly OLDER
   than the newest one already applied is ignored. An unparseable/missing ts is
   treated as OLDEST so it can never defeat this guard and clobber newer state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fm_runtime import JobEvent, JobStatus
from fm_runtime.job_events import TERMINAL_STATUSES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

logger = logging.getLogger(__name__)


def _parse_ts(raw: str) -> datetime | None:
    """Parse an ISO-8601 event timestamp to an aware UTC datetime, or ``None``
    when the producer emits an unparseable/missing ts.

    ``None`` (not now()) is the correct lenient fallback: the caller treats a
    ts-less event as the OLDEST possible, so a bad ts can never win the ordering
    guard and regress newer state. now() would do the opposite — it would always
    look newest and clobber whatever is stored."""
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def apply_event(session: AsyncSession, app: str, event: JobEvent) -> Job | None:
    """Upsert the Job row for ``(app, event.job_id)`` from ``event``.

    Returns the row (created or updated), or ``None`` when the event was
    ignored as a stale replay. Commits within the caller's transaction scope.
    """
    if not event.job_id:
        logger.warning("Dropping job event from %s with empty job_id", app)
        return None

    event_ts = _parse_ts(event.ts)
    status = event.status if isinstance(event.status, JobStatus) else JobStatus.coerce(event.status)

    result = await session.execute(
        select(Job).where(Job.app == app, Job.external_job_id == event.job_id)
    )
    job = result.scalar_one_or_none()

    if job is None:
        job = Job(app=app, external_job_id=event.job_id)
        session.add(job)
    else:
        # Guard 1 — terminal is absorbing. Once terminal, drop any non-terminal
        # event outright (no ts comparison): a job never leaves a terminal
        # state. This also handles equal-ts replays the ts guard would admit.
        stored_status = JobStatus.coerce(job.status) if job.status else None
        if stored_status in TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
            return None
        # Guard 2 — timestamp ordering. Ignore an event strictly older than the
        # newest applied. A ts-less event (event_ts is None) is treated as the
        # OLDEST possible, so it can never clobber newer stored state.
        if job.last_event_at is not None:
            if event_ts is None:
                return None
            stored = job.last_event_at
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=timezone.utc)
            if event_ts < stored:
                return None

    # A concrete timestamp for the recorded fields below; only the ORDERING
    # guards above may treat a bad ts as oldest — the row still needs a value.
    applied_ts = event_ts if event_ts is not None else datetime.now(timezone.utc)

    # --- apply the event (newest-wins fields) -----------------------------
    job.type = event.type or job.type
    job.user = event.user or job.user
    job.origin = event.origin or job.origin
    job.actor = event.actor or job.actor
    job.status = status.value
    if event.progress is not None:
        job.progress = event.progress
    if event.exit_status is not None:
        job.exit_status = event.exit_status
    # Merge meta additively so a later partial event does not drop earlier keys.
    if event.meta:
        merged = dict(job.meta or {})
        merged.update(event.meta)
        job.meta = merged
    elif job.meta is None:
        job.meta = {}

    if job.started_at is None and status not in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
        # First time we see it doing anything past not-yet-running — record a
        # start. QUEUED and SCHEDULED are both pre-execution (a SCHEDULED job is
        # a persisted schedule awaiting its next_run_at); real work has not begun,
        # so do not stamp started_at. A later RUNNING event stamps it when the
        # schedule fires.
        job.started_at = applied_ts
    if status in TERMINAL_STATUSES and job.ended_at is None:
        job.ended_at = applied_ts

    job.last_event_at = applied_ts
    await session.commit()
    return job
