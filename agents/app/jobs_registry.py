"""In-process job registry + event broadcast for the ``/internal/jobs/v1`` stream.

The ``agents`` service is a **v1 jobs producer**: every runtime-AI-agent run
(``AgentRun``) is a job, and its lifecycle transitions are published here as
``JobEvent``s. The internal stream endpoint (``GET /internal/jobs/v1/stream``)
fans them out to the ``jobs`` service, which persists job state and offers
cross-user visibility + pause/resume/cancel control.

Design notes (mirrors ``search``'s registry so the two producers behave
identically for the ``jobs`` subscriber):

- ``job_id`` is the ``AgentRun.id`` (a uuid4 hex) — the exact handle the agents
  service uses for control, so pausing/cancelling a job maps 1:1 onto the run.
- ``JobEvent`` is constructed **strictly per fm_runtime** (real ``JobStatus``
  enums, never bare status strings) so a typo fails loud in-process rather than
  silently degrading a run's reported lifecycle.
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

from app.config import get_settings
from app.models import JOB_TYPE_AGENT_TURN


@dataclass
class JobContext:
    """Attribution carried alongside a run's job events.

    ``origin`` is always ``agent`` for a runtime-agent run; ``actor`` is the
    agents service client (``azp``) that acts downstream through MCP. Together
    they render "alice (via agent)"."""

    user: str
    origin: str
    actor: str


class JobRegistry:
    """Broadcast hub: latest-event-per-job snapshot + live fan-out to subscribers."""

    def __init__(self, terminal_retention_seconds: float) -> None:
        self._lock = asyncio.Lock()
        self._latest: dict[str, JobEvent] = {}
        self._terminal_at: dict[str, float] = {}
        self._subscribers: set[asyncio.Queue[JobEvent]] = set()
        self._retention = terminal_retention_seconds

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            job_id
            for job_id, at in self._terminal_at.items()
            if (now - at) >= self._retention
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
            snapshot = [event for event in self._latest.values() if not event.is_terminal]
            self._subscribers.add(queue)
        try:
            for event in snapshot:
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


job_registry = JobRegistry(get_settings().job_terminal_retention_seconds)


async def publish_job(
    *,
    job_id: str,
    ctx: JobContext,
    status: JobStatus,
    progress: float | None = None,
    exit_status: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Construct a JobEvent (strict) for a run and broadcast it."""
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
    await job_registry.publish(event)


__all__ = ["JobContext", "JobRegistry", "job_registry", "publish_job"]
