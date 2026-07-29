"""In-memory stream jobs for long-running Apollo search ingest.

Designed so one HTTP connection can multiplex many ``stream_id``s: every
event is tagged with ``stream_id``, and ``POST /api/leads/stream`` merges
events from multiple jobs onto a single NDJSON response.

Events are appended to a per-job history so subscribers that connect after
the job starts still receive the full sequence (no missed early pages).

After a job finishes, ``stream_id`` stays resolvable for a short grace period
so in-flight clients can finish/reconnect, then the job (and its buffers) are
dropped to avoid unbounded memory growth.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable

from app.config import get_settings

logger = logging.getLogger(__name__)

# Apollo People/Org search max page size is 100; walk enough pages for 100k entries.
_APOLLO_MAX_PER_PAGE = 100
_MAX_SEARCH_ENTRIES = 100_000
_MAX_APOLLO_PAGES = _MAX_SEARCH_ENTRIES // _APOLLO_MAX_PER_PAGE  # 1000

# Keep finished stream_ids usable briefly, then drop buffers.
# Jobs whose runner never started (still PENDING) use the same window.
_POST_JOB_TTL_SECONDS = 90
_STALE_JOB_MAX_AGE_SECONDS = _POST_JOB_TTL_SECONDS
_POLL_INTERVAL = 0.05


class StreamJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"


def _set_event() -> asyncio.Event:
    """A fresh, already-set event — the default ``_resume`` gate (not paused)."""
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class StreamJob:
    stream_id: str
    status: StreamJobStatus = StreamJobStatus.PENDING
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    total_ids: int = 0
    error: str | None = None
    subscribers: int = 0
    cancelled: bool = False
    paused: bool = False
    # Cleared while paused; set to release the ingest loop (resume or cancel).
    _resume: asyncio.Event = field(default_factory=_set_event, repr=False)
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    _cleanup_handle: asyncio.TimerHandle | None = field(default=None, repr=False)


class StreamJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, StreamJob] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> StreamJob:
        await self.cleanup_expired()
        stream_id = uuid.uuid4().hex
        job = StreamJob(stream_id=stream_id)
        async with self._lock:
            self._jobs[stream_id] = job
        # Drop jobs whose runner never starts; running jobs are rescheduled instead.
        self._schedule_expiry(job, delay=_STALE_JOB_MAX_AGE_SECONDS)
        return job

    def get(self, stream_id: str) -> StreamJob | None:
        job = self._jobs.get(stream_id)
        if job is None:
            return None
        # Treat as gone once past TTL; scheduled cleanup frees the buffers.
        if self._is_expired(job, time.time()):
            return None
        return job

    async def publish(self, stream_id: str, event: dict[str, Any]) -> None:
        job = self._jobs.get(stream_id)
        if not job:
            return
        payload = {"stream_id": stream_id, **event}
        async with job._condition:
            job.history.append(payload)
            job._condition.notify_all()

    async def finish(
        self,
        stream_id: str,
        *,
        status: StreamJobStatus,
        error: str | None = None,
    ) -> None:
        job = self._jobs.get(stream_id)
        if not job:
            return
        async with job._condition:
            job.status = status
            job.error = error
            job.finished_at = time.time()
            job._condition.notify_all()
        # Full history is kept until the job is dropped so subscribers that
        # reconnect within the TTL still receive every event.
        self._schedule_expiry(job)

    def release_subscriber(self, job: StreamJob) -> None:
        job.subscribers = max(0, job.subscribers - 1)
        if job.subscribers == 0:
            self._schedule_expiry(job)

    def _schedule_expiry(self, job: StreamJob, *, delay: float | None = None) -> None:
        now = time.time()
        if delay is None:
            if job.finished_at is not None:
                delay = max(0.0, _POST_JOB_TTL_SECONDS - (now - job.finished_at))
            else:
                delay = max(0.0, _STALE_JOB_MAX_AGE_SECONDS - (now - job.created_at))
        if job._cleanup_handle is not None:
            job._cleanup_handle.cancel()
            job._cleanup_handle = None

        loop = asyncio.get_running_loop()

        def _fire() -> None:
            job._cleanup_handle = None
            asyncio.create_task(self._expire_job(job.stream_id))

        job._cleanup_handle = loop.call_later(delay, _fire)

    async def _expire_job(self, stream_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(stream_id)
            if not job:
                return
            if self._is_expired(job, time.time()):
                self._drop_job(job)
                return
            # Still running or subscribed — check again after the grace window.
            self._schedule_expiry(job, delay=_POST_JOB_TTL_SECONDS)

    @staticmethod
    def _is_expired(job: StreamJob, now: float) -> bool:
        if job.subscribers > 0:
            return False
        if job.status in {StreamJobStatus.COMPLETE, StreamJobStatus.ERROR}:
            return job.finished_at is not None and now - job.finished_at >= _POST_JOB_TTL_SECONDS
        if job.status is StreamJobStatus.PENDING:
            # Created but its runner never started — safe to drop.
            return now - job.created_at >= _STALE_JOB_MAX_AGE_SECONDS
        # RUNNING jobs never expire: dropping one would orphan a live Apollo
        # ingest (publish/finish become no-ops and cancel returns 404) while it
        # keeps spending credits. finish() starts the TTL when the job ends.
        return False

    def _drop_job(self, job: StreamJob) -> None:
        if job._cleanup_handle is not None:
            job._cleanup_handle.cancel()
            job._cleanup_handle = None
        job.history.clear()
        self._jobs.pop(job.stream_id, None)
        logger.debug("Dropped stream job %s", job.stream_id)

    async def cleanup_expired(self) -> None:
        now = time.time()
        async with self._lock:
            expired = [
                job
                for job in list(self._jobs.values())
                if self._is_expired(job, now)
            ]
            for job in expired:
                self._drop_job(job)


stream_job_manager = StreamJobManager()


@dataclass
class EmbeddingStreamState:
    """Tracks embedding batches for one search against a stream job id."""

    stream_id: str
    total: int = 0
    done: int = 0
    in_flight: int = 0
    accepting: bool = True
    failed: bool = False
    cancelled: bool = False
    paused: bool = False
    # Cleared while paused; set to release embedding chunks (resume or cancel).
    _resume: asyncio.Event = field(default_factory=_set_event, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_embedding_states: dict[str, EmbeddingStreamState] = {}


# Control-hook status labels (leads is the engine; search maps these onto job
# lifecycle). Deliberately terse data — no UI shaping happens here.
CONTROL_STATUS_RUNNING = "running"
CONTROL_STATUS_PAUSED = "paused"
CONTROL_STATUS_CANCELED = "canceled"
CONTROL_STATUS_COMPLETE = "complete"
CONTROL_STATUS_ERROR = "error"


def _job_status_label(job: StreamJob) -> str:
    if job.status is StreamJobStatus.COMPLETE:
        return CONTROL_STATUS_COMPLETE
    if job.status is StreamJobStatus.ERROR:
        return CONTROL_STATUS_ERROR
    if job.cancelled:
        return CONTROL_STATUS_CANCELED
    if job.paused:
        return CONTROL_STATUS_PAUSED
    return CONTROL_STATUS_RUNNING


def _embedding_status_label(state: EmbeddingStreamState) -> str:
    if state.failed:
        return CONTROL_STATUS_ERROR
    if state.cancelled:
        return CONTROL_STATUS_CANCELED
    if state.paused:
        return CONTROL_STATUS_PAUSED
    return CONTROL_STATUS_RUNNING


async def cancel_stream(stream_id: str) -> bool:
    """Request cancellation of an ingest or embedding stream job.

    Returns True if a running job was marked cancelled (or embedding was closed).
    """
    cleaned = str(stream_id or "").strip()
    if not cleaned:
        return False

    state = _embedding_states.get(cleaned)
    if state is not None:
        async with state._lock:
            if state.failed or state.cancelled:
                return False
            state.cancelled = True
            state.accepting = False
            # Wake any chunk waiting on a pause so it observes the cancel.
            state._resume.set()
            done = state.done
            total = state.total
        await stream_job_manager.publish(
            cleaned,
            {
                "type": "complete",
                "kind": "embedding",
                "cancelled": True,
                "done": done,
                "total": total,
            },
        )
        await stream_job_manager.finish(cleaned, status=StreamJobStatus.COMPLETE)
        _embedding_states.pop(cleaned, None)
        return True

    job = stream_job_manager.get(cleaned)
    if job is None or job.status in {StreamJobStatus.COMPLETE, StreamJobStatus.ERROR}:
        return False
    async with job._condition:
        if job.cancelled:
            return False
        job.cancelled = True
        # Release a paused ingest loop so it observes the cancel and stops.
        job._resume.set()
        job._condition.notify_all()
    return True


async def set_stream_paused(stream_id: str, paused: bool) -> tuple[bool, str | None]:
    """Pause or resume an ingest or embedding stream job.

    Mirrors ``cancel_stream``: resolves ``stream_id`` to an embedding state first,
    else an ingest job. Idempotent — re-pausing/re-resuming is a no-op that still
    reports the current status. Returns ``(found, status_label)`` where ``found``
    is False for an unknown/finished stream (the caller maps that to 404) and
    ``status_label`` is one of the ``CONTROL_STATUS_*`` values.
    """
    cleaned = str(stream_id or "").strip()
    if not cleaned:
        return False, None

    state = _embedding_states.get(cleaned)
    if state is not None:
        async with state._lock:
            if state.failed or state.cancelled:
                # Terminal already — nothing to toggle.
                return False, _embedding_status_label(state)
            state.paused = paused
            if paused:
                state._resume.clear()
            else:
                state._resume.set()
            label = _embedding_status_label(state)
        await stream_job_manager.publish(
            cleaned,
            {
                "type": "paused" if paused else "resumed",
                "kind": "embedding",
                "done": state.done,
                "total": state.total,
            },
        )
        return True, label

    job = stream_job_manager.get(cleaned)
    if job is None or job.status in {StreamJobStatus.COMPLETE, StreamJobStatus.ERROR}:
        return False, None
    async with job._condition:
        if job.cancelled:
            return False, _job_status_label(job)
        job.paused = paused
        if paused:
            job._resume.clear()
        else:
            job._resume.set()
        job._condition.notify_all()
        label = _job_status_label(job)
    await stream_job_manager.publish(
        cleaned,
        {
            "type": "paused" if paused else "resumed",
            "kind": "ingest",
            "stored": job.total_ids,
        },
    )
    return True, label


def stream_status(stream_id: str) -> str | None:
    """Current control status of a stream (``None`` if unknown/expired)."""
    cleaned = str(stream_id or "").strip()
    if not cleaned:
        return None
    state = _embedding_states.get(cleaned)
    if state is not None:
        return _embedding_status_label(state)
    job = stream_job_manager.get(cleaned)
    if job is None:
        return None
    return _job_status_label(job)


async def create_embedding_stream() -> str:
    """Create a stream job used for embedding progress events.

    Does not publish a 0/0 progress tick — subscribers should wait for the first
    ``schedule_embedding_batch`` progress (or complete) so UIs do not show a
    stuck 0% ring while Apollo ingest/match is still running.
    """
    job = await stream_job_manager.create()
    job.status = StreamJobStatus.RUNNING
    _embedding_states[job.stream_id] = EmbeddingStreamState(stream_id=job.stream_id)
    return job.stream_id


async def _maybe_finish_embedding(state: EmbeddingStreamState) -> None:
    if state.failed:
        return
    if state.accepting or state.in_flight > 0:
        return
    await stream_job_manager.publish(
        state.stream_id,
        {
            "type": "complete",
            "kind": "embedding",
            "total": state.done,
            "done": state.done,
        },
    )
    await stream_job_manager.finish(state.stream_id, status=StreamJobStatus.COMPLETE)
    _embedding_states.pop(state.stream_id, None)


async def close_embedding_stream(stream_id: str) -> None:
    """Mark that no further embedding batches will be scheduled."""
    state = _embedding_states.get(stream_id)
    if not state:
        return
    async with state._lock:
        state.accepting = False
        await _maybe_finish_embedding(state)


# Embed in small sub-batches so `done` climbs mid-batch instead of jumping
# 0 -> total at the end (which leaves the ring spinning with no visible percent).
_EMBED_PROGRESS_CHUNK = 16


async def schedule_embedding_batch(
    stream_id: str,
    mongo_ids: list[str],
    *,
    embed_batch: Callable[[list[str]], Awaitable[list[str]]],
) -> None:
    """Embed ``mongo_ids`` and publish incremental progress on ``stream_id``.

    Awaits the batch so a peer embedding consumer can own concurrency via
    ``asyncio.gather`` / a queue rather than fire-and-forget child tasks. The
    ids are embedded in small chunks and ``done`` is advanced after each chunk
    so the UI shows a rising percentage rather than a stuck spinner.
    """
    unique_ids = list(dict.fromkeys(str(item).strip() for item in mongo_ids if str(item or "").strip()))
    state = _embedding_states.get(stream_id)
    if not state or not unique_ids:
        if state and not state.accepting and not state.cancelled:
            async with state._lock:
                await _maybe_finish_embedding(state)
        return

    async with state._lock:
        if not state.accepting or state.cancelled:
            return
        state.total += len(unique_ids)
        state.in_flight += 1
        total = state.total
        done = state.done

    await stream_job_manager.publish(
        stream_id,
        {"type": "progress", "kind": "embedding", "done": done, "total": total},
    )

    async def _finish_in_flight() -> None:
        state_inner = _embedding_states.get(stream_id)
        if not state_inner:
            return
        async with state_inner._lock:
            state_inner.in_flight = max(0, state_inner.in_flight - 1)
            if state_inner.cancelled:
                return
            await _maybe_finish_embedding(state_inner)

    for start in range(0, len(unique_ids), _EMBED_PROGRESS_CHUNK):
        # Hold before spending the next chunk of OpenAI/Milvus work while paused;
        # resume() or cancel() sets the event. cancel_stream sets it too so a
        # paused-then-cancelled batch is released and observes the cancel below.
        pause_state = _embedding_states.get(stream_id)
        if pause_state is not None and pause_state.paused and not pause_state.cancelled:
            await pause_state._resume.wait()
        gate_state = _embedding_states.get(stream_id)
        if gate_state is None or gate_state.cancelled:
            return
        chunk = unique_ids[start : start + _EMBED_PROGRESS_CHUNK]
        try:
            # Soft-failures inside embed_batch return fewer ids; still advance by chunk size.
            await embed_batch(chunk)
        except Exception:
            logger.exception("Embedding batch failed for stream %s", stream_id)
            state_inner = _embedding_states.get(stream_id)
            if not state_inner:
                return
            async with state_inner._lock:
                state_inner.in_flight = max(0, state_inner.in_flight - 1)
                if state_inner.cancelled:
                    return
                state_inner.failed = True
                await stream_job_manager.publish(
                    stream_id,
                    {"type": "error", "kind": "embedding", "detail": "Embedding batch failed"},
                )
                await stream_job_manager.finish(
                    stream_id, status=StreamJobStatus.ERROR, error="Embedding batch failed"
                )
                _embedding_states.pop(stream_id, None)
            return

        state_inner = _embedding_states.get(stream_id)
        if not state_inner:
            return
        async with state_inner._lock:
            if state_inner.cancelled:
                return
            state_inner.done += len(chunk)
            done_now = state_inner.done
            total_now = state_inner.total
        await stream_job_manager.publish(
            stream_id,
            {"type": "progress", "kind": "embedding", "done": done_now, "total": total_now},
        )

    await _finish_in_flight()


def _pagination_total_pages(
    apollo_raw: dict[str, Any],
    *,
    per_page: int,
    result_count: int,
) -> int:
    nested = dict(apollo_raw.get("pagination") or {})
    total = int(
        nested.get("total_entries")
        or apollo_raw.get("total_entries")
        or result_count
        or 0
    )
    # Cap reported totals at our ingest ceiling for progress UI.
    total = min(total, _MAX_SEARCH_ENTRIES) if total > 0 else total
    explicit_total_pages = nested.get("total_pages") or apollo_raw.get("total_pages")
    if explicit_total_pages is not None and str(explicit_total_pages).strip() != "":
        total_pages = max(1, int(explicit_total_pages))
    elif total > 0:
        total_pages = max(1, (total + per_page - 1) // per_page)
    else:
        total_pages = 1
    return min(total_pages, _MAX_APOLLO_PAGES)


PageFetchFn = Callable[[dict[str, Any]], Awaitable[tuple[list[str], dict[str, Any], int]]]
EmbedBatchFn = Callable[[list[str]], Awaitable[list[str]]]


async def run_paged_search_stream(
    *,
    stream_id: str,
    base_params: dict[str, Any],
    fetch_page: PageFetchFn,
    schedule_embed: Callable[[list[str]], Awaitable[None]] | None = None,
    on_ingest_complete: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Walk Apollo pages at max page size, publishing ``ids`` events, then ``complete`` / ``error``.

    Always requests ``per_page=100`` (Apollo max) and stops at ``_MAX_SEARCH_ENTRIES`` (100k)
    or when Apollo reports no further pages.

    Prefer ``run_paged_search_with_embedding`` for streamed searches so ingest and
    embedding run as peer tasks. ``schedule_embed`` here should only enqueue work
    (e.g. ``queue.put``), not await OpenAI/Milvus.
    """
    manager = stream_job_manager
    job = manager.get(stream_id)
    if not job:
        return
    job.status = StreamJobStatus.RUNNING

    try:
        page = max(1, int(base_params.get("page") or 1))
        per_page = _APOLLO_MAX_PER_PAGE
        total_pages = 1

        while page <= _MAX_APOLLO_PAGES and job.total_ids < _MAX_SEARCH_ENTRIES:
            if job.cancelled:
                break
            if job.paused:
                # Hold before spending the next Apollo page of credits; resume()
                # or cancel() sets the event. Pause takes effect within one page.
                await job._resume.wait()
                if job.cancelled:
                    break
            page_params = {**base_params, "page": page, "per_page": per_page}
            mongo_ids, apollo_raw, result_count = await fetch_page(page_params)
            if job.cancelled:
                break
            total_pages = _pagination_total_pages(
                apollo_raw,
                per_page=per_page,
                result_count=result_count,
            )

            if mongo_ids:
                remaining = _MAX_SEARCH_ENTRIES - job.total_ids
                if len(mongo_ids) > remaining:
                    mongo_ids = mongo_ids[:remaining]
                job.total_ids += len(mongo_ids)
                if schedule_embed is not None and not job.cancelled:
                    await schedule_embed(mongo_ids)
                await manager.publish(
                    stream_id,
                    {
                        "type": "ids",
                        "kind": "ingest",
                        "page": page,
                        "total_pages": total_pages,
                        "ids": mongo_ids,
                        "stored": job.total_ids,
                    },
                )

            if (
                job.cancelled
                or not mongo_ids
                or job.total_ids >= _MAX_SEARCH_ENTRIES
                or page >= total_pages
                or result_count < per_page
            ):
                break
            page += 1

        await manager.publish(
            stream_id,
            {
                "type": "complete",
                "kind": "ingest",
                "total": job.total_ids,
                "pages": page,
                "cancelled": job.cancelled,
            },
        )
        await manager.finish(stream_id, status=StreamJobStatus.COMPLETE)
        if on_ingest_complete is not None:
            await on_ingest_complete()
    except Exception as exc:
        logger.exception("Stream job %s failed", stream_id)
        await manager.publish(
            stream_id,
            {"type": "error", "kind": "ingest", "detail": str(exc)},
        )
        await manager.finish(stream_id, status=StreamJobStatus.ERROR, error=str(exc))
        if on_ingest_complete is not None:
            await on_ingest_complete()


async def run_paged_search_with_embedding(
    *,
    ingest_stream_id: str,
    embedding_stream_id: str,
    base_params: dict[str, Any],
    fetch_page: PageFetchFn,
    embed_batch: EmbedBatchFn,
) -> None:
    """Run Apollo ingest and embedding as peer coroutines linked by a queue.

    Ingest only ``put``s mongo id batches; a sibling consumer embeds them so OpenAI
    / Milvus work overlaps with the next Apollo page fetch instead of waiting for
    the ingest coroutine to finish.
    """
    queue: asyncio.Queue[list[str] | None] = asyncio.Queue()

    async def _ingest() -> None:
        try:
            await run_paged_search_stream(
                stream_id=ingest_stream_id,
                base_params=base_params,
                fetch_page=fetch_page,
                schedule_embed=lambda mongo_ids: queue.put(list(mongo_ids)),
                on_ingest_complete=None,
            )
        finally:
            # Sentinel: embedding consumer drains remaining batches then closes.
            await queue.put(None)

    async def _embed_consumer() -> None:
        # How many page-sized embedding batches may run at once while ingest
        # continues (env-tunable). Only the short Milvus upsert inside each batch
        # serializes on the priority gate; the OpenAI embed overlaps freely.
        concurrency = max(1, int(get_settings().embed_batch_concurrency))
        sem = asyncio.Semaphore(concurrency)
        tasks: set[asyncio.Task[None]] = set()

        async def _one(batch: list[str]) -> None:
            async with sem:
                await schedule_embedding_batch(
                    embedding_stream_id,
                    batch,
                    embed_batch=embed_batch,
                )

        try:
            while True:
                batch = await queue.get()
                if batch is None:
                    break
                state = _embedding_states.get(embedding_stream_id)
                # Drain the queue without work once cancelled so ingest can finish.
                if state is None or state.cancelled:
                    continue
                task = asyncio.create_task(
                    _one(batch),
                    name=f"embed-batch-{embedding_stream_id[:8]}",
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # cancel_stream may have already finished the embedding job.
            if embedding_stream_id in _embedding_states:
                await close_embedding_stream(embedding_stream_id)

    await asyncio.gather(_ingest(), _embed_consumer())


async def iter_stream_events(stream_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield events from one or more stream jobs until each has finished."""
    manager = stream_job_manager
    jobs: list[StreamJob] = []
    missing: list[str] = []
    seen_ids: set[str] = set()

    for stream_id in stream_ids:
        cleaned = str(stream_id or "").strip()
        if not cleaned or cleaned in seen_ids:
            continue
        seen_ids.add(cleaned)
        job = manager.get(cleaned)
        if job is None:
            missing.append(cleaned)
        else:
            jobs.append(job)

    for stream_id in missing:
        yield {
            "stream_id": stream_id,
            "type": "error",
            "detail": "Unknown or expired stream_id",
        }

    if not jobs:
        return

    for job in jobs:
        job.subscribers += 1

    cursors = {job.stream_id: 0 for job in jobs}
    pending = {job.stream_id: job for job in jobs}

    try:
        while pending:
            progressed = False
            # Round-robin one event per job so embedding progress is not stuck
            # behind a backlog of ingest ``ids`` events.
            for stream_id, job in list(pending.items()):
                cursor = cursors[stream_id]
                if cursor >= len(job.history):
                    if job.status in {
                        StreamJobStatus.COMPLETE,
                        StreamJobStatus.ERROR,
                    }:
                        pending.pop(stream_id, None)
                    continue
                event = job.history[cursor]
                cursors[stream_id] = cursor + 1
                progressed = True
                yield event
                if event.get("type") in {"complete", "error"}:
                    pending.pop(stream_id, None)

            if not pending:
                break
            if not progressed:
                # Wait for any pending job to publish (race all conditions).
                waiters = list(pending.values())
                if not waiters:
                    break

                async def _wait_one(j: StreamJob) -> None:
                    async with j._condition:
                        await j._condition.wait()

                tasks = [
                    asyncio.create_task(_wait_one(job), name=f"stream-wait-{job.stream_id}")
                    for job in waiters
                ]
                try:
                    _done, remaining = await asyncio.wait(
                        tasks,
                        timeout=_POLL_INTERVAL,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for job in jobs:
            manager.release_subscriber(job)
