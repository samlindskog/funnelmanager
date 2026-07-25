"""Producer-stream subscriber framework.

For every CONFIG-DRIVEN producer (``JOBS_PRODUCERS``, parsed by
``Settings.producer_map``) this starts one long-lived background task that tails
the producer's ``GET /internal/jobs/v1/stream`` (NDJSON of
:class:`fm_runtime.JobEvent`) and upserts Job rows via :mod:`app.store`.

Design rules from the build plan:

- **Exchange, never forward.** Each subscriber calls through
  :class:`fm_runtime.InternalClient` with ``audience = <producer name>`` and NO
  request principal, so the broker mints a client-credentials token for this
  service's own identity (``azp = jobs``) scoped to the producer's audience
  (``svc-<producer>`` optional client scope — v1: ``jobs->search``,
  ``jobs->agents``). The producer authorizes the ``jobs`` caller on its
  internal stream; the stream is READ-ONLY.
- **Tolerate an absent/unreachable producer.** v1 producers (`search`,
  `agents`) do not exist yet. A connection that fails or ends reconnects with
  capped exponential backoff; the task NEVER crashes and never brings down the
  service.
- **Replay-safe.** The producer buffers per-job history, so a reconnect re-reads
  a job's whole lifecycle; :func:`app.store.apply_event` gates on the event
  timestamp so redelivered/out-of-order events never regress a terminal state.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fm_runtime import ExchangeError, InternalClient, JobEvent
from fm_runtime.job_events import JOBS_STREAM_PATH

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.store import apply_event

logger = logging.getLogger(__name__)

# A streaming GET stays open between events, so the read timeout must be
# unbounded (a bounded read would tear an idle-but-healthy stream down every
# timeout). Connect/write/pool stay bounded so a dead peer is still detected.
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=None)


class ProducerSubscriber:
    """Tails one producer's job-event stream, reconnecting forever."""

    def __init__(self, name: str, base_url: str, settings: Settings) -> None:
        self.name = name
        self.base_url = base_url
        self._settings = settings
        self._stop = asyncio.Event()
        self._backoff = settings.subscriber_backoff_initial_seconds

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._consume_once()
                # Clean end of stream (producer closed it): reconnect promptly.
                self._backoff = self._settings.subscriber_backoff_initial_seconds
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ExchangeError) as exc:
                # Producer down/absent/unreachable, or exchange refused — the
                # expected steady state until search/agents land. Stay quiet-ish
                # and back off; never crash.
                logger.warning(
                    "jobs subscriber for %s disconnected (%s); retrying in %.1fs",
                    self.name,
                    exc,
                    self._backoff,
                )
            except Exception:
                logger.exception(
                    "jobs subscriber for %s hit an unexpected error; retrying in %.1fs",
                    self.name,
                    self._backoff,
                )
            await self._sleep_or_stop(self._backoff)
            self._backoff = min(
                self._backoff * 2, self._settings.subscriber_backoff_max_seconds
            )

    async def _consume_once(self) -> None:
        """Open the stream and apply every event until it ends or errors."""
        async with InternalClient(
            self.base_url, audience=self.name, timeout=_STREAM_TIMEOUT
        ) as client:
            first = True
            async for line in client.stream_lines("GET", JOBS_STREAM_PATH):
                if self._stop.is_set():
                    return
                if first:
                    # Connected successfully — reset backoff so a later mid-stream
                    # drop reconnects fast rather than at the grown interval.
                    self._backoff = self._settings.subscriber_backoff_initial_seconds
                    first = False
                    logger.info("jobs subscriber connected to %s stream", self.name)
                line = line.strip()
                if not line:
                    continue
                await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        try:
            event = JobEvent.from_json_line(line)
        except (ValueError, TypeError) as exc:
            # A malformed line must not kill the stream — skip it.
            logger.warning("jobs subscriber for %s got a bad event line: %s", self.name, exc)
            return
        try:
            async with SessionLocal() as session:
                await apply_event(session, self.name, event)
        except Exception:
            logger.exception(
                "jobs subscriber for %s failed to persist event %s", self.name, event.job_id
            )

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def signal_stop(self) -> None:
        self._stop.set()


class SubscriberManager:
    """Owns the per-producer subscriber tasks; started/stopped in the app
    lifespan."""

    def __init__(self) -> None:
        self._subs: list[ProducerSubscriber] = []
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        settings = get_settings()
        if not settings.subscriber_enabled:
            logger.info("jobs subscriber disabled (SUBSCRIBER_ENABLED=false)")
            return
        producers = settings.producer_map
        if not producers:
            logger.info("jobs subscriber: no producers configured (JOBS_PRODUCERS empty)")
            return
        for name, base_url in producers.items():
            sub = ProducerSubscriber(name, base_url, settings)
            self._subs.append(sub)
            self._tasks.append(asyncio.create_task(sub.run(), name=f"jobs-subscriber-{name}"))
        logger.info("jobs subscriber started for producers: %s", ", ".join(producers))

    async def stop(self) -> None:
        for sub in self._subs:
            sub.signal_stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._subs.clear()
        self._tasks.clear()


subscriber_manager = SubscriberManager()
