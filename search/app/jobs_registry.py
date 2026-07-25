"""In-process job registry + event broadcast for the ``/internal/jobs/v1`` stream.

Search is a **job producer**: every Apollo ingest, its embedding pass, and each
semantic search publishes lifecycle ``JobEvent``s here. The internal stream
endpoint (``GET /internal/jobs/v1/stream``) fans them out to the ``jobs`` service,
which persists job state and offers cross-user visibility + control.

Design notes:
- ``job_id`` is the underlying **leads stream id** (ingest or embedding). That
  makes control trivial and unambiguous: pausing/cancelling a job maps 1:1 onto
  the leads stream control hook, and the id the jobs service stores is the exact
  handle search uses upstream. Semantic searches have no leads stream, so they
  get a synthetic ``sem-<search_id>`` id and only ever emit a terminal event.
- ``JobEvent`` is constructed **strictly per fm_runtime** (real ``JobStatus``
  enums, no bare status strings) so a typo fails loud in-process rather than
  silently degrading a job's reported lifecycle.
- The registry keeps the latest event per job so a subscriber that connects mid
  run is replayed the current state of every still-running job. Terminal jobs are
  pruned after a short TTL (the live terminal event already reached subscribers).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fm_runtime import JobEvent, JobStatus

# How long a terminal job's latest event lingers for late-subscriber replay
# before it is pruned. The live terminal event still reaches every currently
# connected subscriber immediately; this only bounds the snapshot memory.
_TERMINAL_RETENTION_SECONDS = 120.0

# Job type vocabulary (search owns its own; free-form on the wire).
JOB_TYPE_APOLLO_SEARCH = "apollo_search"
JOB_TYPE_EMBEDDING = "embedding"
JOB_TYPE_SEMANTIC_SEARCH = "semantic_search"


@dataclass
class JobContext:
    """Attribution + linkage carried alongside a search's job events."""

    user: str
    origin: str
    actor: str
    search_id: int
    job_type: str = JOB_TYPE_APOLLO_SEARCH


class JobRegistry:
    """Broadcast hub: latest-event-per-job snapshot + live fan-out to subscribers."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._latest: dict[str, JobEvent] = {}
        self._terminal_at: dict[str, float] = {}
        self._subscribers: set[asyncio.Queue[JobEvent]] = set()

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            job_id
            for job_id, at in self._terminal_at.items()
            if (now - at) >= _TERMINAL_RETENTION_SECONDS
        ]
        for job_id in stale:
            self._latest.pop(job_id, None)
            self._terminal_at.pop(job_id, None)

    async def publish(self, event: JobEvent) -> None:
        async with self._lock:
            self._prune_locked()
            self._latest[event.job_id] = event
            if event.is_terminal:
                self._terminal_at[event.job_id] = time.monotonic()
            else:
                self._terminal_at.pop(event.job_id, None)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            # Unbounded put_nowait: subscribers drain promptly, and dropping a
            # lifecycle event would desync the jobs store worse than brief growth.
            queue.put_nowait(event)

    async def latest(self, job_id: str) -> JobEvent | None:
        async with self._lock:
            return self._latest.get(job_id)

    async def subscribe(self) -> AsyncIterator[JobEvent]:
        """Yield a snapshot of every still-running job, then live events."""
        queue: asyncio.Queue[JobEvent] = asyncio.Queue()
        async with self._lock:
            self._prune_locked()
            snapshot = [
                event
                for event in self._latest.values()
                if not event.is_terminal
            ]
            self._subscribers.add(queue)
        try:
            for event in snapshot:
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


job_registry = JobRegistry()


async def publish_job(
    *,
    job_id: str,
    job_type: str,
    ctx: JobContext | None,
    status: JobStatus,
    progress: float | None = None,
    exit_status: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Construct a JobEvent (strict) and broadcast it. No-op without a ctx."""
    if ctx is None or not str(job_id or "").strip():
        return
    event = JobEvent(
        job_id=str(job_id),
        type=job_type,
        user=ctx.user,
        origin=ctx.origin,
        actor=ctx.actor,
        status=status,
        progress=progress,
        exit_status=exit_status,
        meta={"search_id": ctx.search_id, **(meta or {})},
    )
    await job_registry.publish(event)


def job_progress_fraction(done: int, total: int) -> float | None:
    """Clamp done/total to 0.0-1.0, or None when total is unknown."""
    if total <= 0:
        return None
    return max(0.0, min(1.0, done / total))


__all__ = [
    "JOB_TYPE_APOLLO_SEARCH",
    "JOB_TYPE_EMBEDDING",
    "JOB_TYPE_SEMANTIC_SEARCH",
    "JobContext",
    "JobRegistry",
    "job_progress_fraction",
    "job_registry",
    "publish_job",
]
