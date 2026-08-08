"""Shared producer-side machinery for the ``/internal/jobs/v1`` contract.

This is the reusable chassis behind the **standardized Jobs Producer API**: the
single piece of plumbing every service implements so the ``jobs`` service can
view and control that service's in-flight work. It lifts the boilerplate that
was hand-copied into ``search/app/jobs_registry.py`` and
``agents/app/jobs_registry.py`` into **one** place (P10 — cross-cutting plumbing
belongs in ``fm_runtime``, not re-implemented per service). ``search`` is migrated
onto it (``search/app/jobs_registry.py`` is the reference wiring); ``agents`` still
hand-rolls its own copy and migrates when the agents backend is rebuilt.
``docs/jobs-producer-api.md`` is the authoritative spec and cites this module.

The producer contract (normative)
=================================

A **jobs producer** is any service that exposes in-service processes for the
``jobs`` service to observe and control. It implements exactly two versioned
routes, callable **only by the ``jobs`` service account** (realm role
``jobs-internal``, scoped to ``/internal/jobs`` on the producer; enforced by
``fm_runtime`` grants in compose / OPA in the mesh; never nginx-exposed):

- ``GET /internal/jobs/v1/stream`` (:data:`~fm_runtime.job_events.JOBS_STREAM_PATH`)
  — an NDJSON stream of :class:`~fm_runtime.job_events.JobEvent`. On connect it
  replays a **snapshot of every non-terminal job** (see the surfacing philosophy
  below), then streams live lifecycle events. It **must never raise once the
  response has started** (P8): a stray exception resets the connection and the
  ``jobs`` subscriber sees a generic network error. :meth:`JobProducer.stream_ndjson`
  is the never-raise generator that guarantees this.
- ``POST /internal/jobs/v1/{job_id}/{action}``
  (:data:`~fm_runtime.job_events.JOBS_CONTROL_PATH_TEMPLATE`) — a
  :class:`~fm_runtime.job_events.JobControlAction` (``pause`` / ``resume`` /
  ``cancel``). The producer applies it to its own engine **under its own service
  identity** (the acting human rides only as the ``X-FM-Acting-*`` audit header,
  already authorized at the jobs API), and **re-publishes a ``JobEvent``** so the
  stream stays the single source of truth for job state.
  :meth:`JobProducer.dispatch_control` is the control scaffold that does the
  apply-then-republish.

The shared schema
-----------------

Defined once in :mod:`fm_runtime.job_events` and imported by every side, so
producers and the consumer can never drift:
:class:`~fm_runtime.job_events.JobEvent` (the wire event),
:class:`~fm_runtime.job_events.JobStatus`
(``queued`` / ``running`` / ``paused`` / ``scheduled`` / ``completed`` /
``failed`` / ``canceled``; terminal = the last three), and
:class:`~fm_runtime.job_events.JobControlAction`. First-party producers construct
``JobEvent`` **strictly** (real enums, so a status typo fails loud in-process);
the wire-decode paths on the consumer are lenient (unknown future statuses fall
back to a safe non-terminal value, never dropped).

The surfacing philosophy (normative): *idle emits no job*
---------------------------------------------------------

``jobs`` exists to surface **running / long-running / scheduled** work for
deterministic CPU/work introspection — **never idle state**. So:

- A job row is created **lazily on first event** — when real work actually
  starts, or when a schedule is registered. There is no eager "session exists ⇒
  job" mapping. An idle agent session, a search that has finished, an
  un-triggered feature: **none of these are jobs**.
- The subscribe snapshot replays **only non-terminal jobs** — ``running`` /
  ``paused`` / ``queued`` / ``scheduled``. ``scheduled`` (persisted future work
  that has not begun) is non-terminal and therefore **is** in the snapshot, so a
  late subscriber (or ``jobs`` reconnecting) sees pending schedules.
- Terminal jobs (``completed`` / ``failed`` / ``canceled``) age out of the
  producer's memory after a short TTL (their terminal event already reached every
  connected subscriber); the durable history lives in the ``jobs`` store, not
  here. This bounds producer memory to *active* work.

Registration (how a service becomes a producer)
------------------------------------------------

A producing service constructs one :class:`JobProducer` and supplies **two
engine callbacks** — everything else (the broadcast hub, the active-only
snapshot, the TTL prune, the never-raise NDJSON generator, the control
apply-then-republish) is provided here:

- ``enumerate_active_jobs() -> list[JobEvent]`` — the authoritative snapshot of
  the engine's currently non-terminal jobs. Called on each subscribe. This is
  what lets persisted state (e.g. schedules reloaded after a pod restart) be
  surfaced even before any in-memory event has been published for them. May be
  sync or async. Optional: if omitted, the snapshot is just the hub's own
  in-memory latest-event cache.
- ``apply_control(job_id, action) -> JobEvent | None`` — apply a control action
  to the engine and return the resulting ``JobEvent`` (which
  :meth:`dispatch_control` re-publishes), or ``None`` if the job is unknown /
  the action is a no-op. May be sync or async. Optional: if omitted,
  :meth:`dispatch_control` raises — a producer that offers control must supply it.

Then the service's two thin routers call :meth:`stream_ndjson` and
:meth:`dispatch_control`; producers publish lifecycle transitions with
:meth:`publish`. (Wire-up of the routers themselves is service-local and is
**not** in this module — a service names its own job types, ids, and attribution.)

Dependency-free beyond ``fm_runtime.job_events`` + stdlib ``asyncio``, so it
imports anywhere a producer runs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fm_runtime.job_events import JobControlAction, JobEvent

logger = logging.getLogger(__name__)

#: Default lifetime a terminal job's latest event lingers for late-subscriber
#: replay before it is pruned from producer memory. The live terminal event
#: still reaches every currently connected subscriber immediately; this only
#: bounds the snapshot's memory footprint.
DEFAULT_TERMINAL_RETENTION_SECONDS = 120.0

# A producer's engine callbacks may be sync or async — support both.
EnumerateActiveJobs = Callable[[], list[JobEvent] | Awaitable[list[JobEvent]]]
ApplyControl = Callable[
    [str, JobControlAction], JobEvent | None | Awaitable[JobEvent | None]
]


async def _maybe_await(value: object) -> object:
    """Await ``value`` if it is awaitable, else return it — so a producer's
    engine callback can be either a plain function or a coroutine function."""
    if inspect.isawaitable(value):
        return await value
    return value


class JobProducer:
    """The producer-side broadcast hub + never-raise stream + control scaffold.

    One instance per producing service. Holds the latest ``JobEvent`` per job for
    the active-only subscribe snapshot, fans live events out to N subscribers,
    prunes terminal jobs after a TTL, and owns the two engine callbacks that make
    the snapshot authoritative and control effective.

    Thread-safety: designed for a single asyncio event loop (the FastAPI worker).
    All shared-state mutation is guarded by an ``asyncio.Lock``; fan-out to
    subscriber queues is ``put_nowait`` (unbounded — subscribers drain promptly,
    and dropping a lifecycle event would desync the ``jobs`` store worse than
    brief queue growth).
    """

    def __init__(
        self,
        *,
        enumerate_active_jobs: EnumerateActiveJobs | None = None,
        apply_control: ApplyControl | None = None,
        terminal_retention_seconds: float = DEFAULT_TERMINAL_RETENTION_SECONDS,
        log: logging.Logger | None = None,
    ) -> None:
        self._enumerate_active_jobs = enumerate_active_jobs
        self._apply_control = apply_control
        self._retention = float(terminal_retention_seconds)
        self._log = log or logger
        self._lock = asyncio.Lock()
        self._latest: dict[str, JobEvent] = {}
        self._terminal_at: dict[str, float] = {}
        self._subscribers: set[asyncio.Queue[JobEvent]] = set()

    # -- broadcast hub -----------------------------------------------------

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
        """Record ``event`` as the latest for its job and fan it out live.

        A terminal event stamps the job for TTL pruning; any non-terminal event
        (including ``scheduled``) clears that stamp so the job stays in the
        active snapshot."""
        async with self._lock:
            self._prune_locked()
            self._latest[event.job_id] = event
            if event.is_terminal:
                self._terminal_at[event.job_id] = time.monotonic()
            else:
                self._terminal_at.pop(event.job_id, None)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            queue.put_nowait(event)

    async def latest(self, job_id: str) -> JobEvent | None:
        """The most recent event seen for ``job_id`` (or ``None``)."""
        async with self._lock:
            return self._latest.get(job_id)

    async def _snapshot_active(self) -> list[JobEvent]:
        """Build the active-only snapshot: the hub's own non-terminal latest
        cache, unioned with the engine's ``enumerate_active_jobs()`` if supplied.

        The in-memory latest event wins on a job-id collision (it reflects the
        freshest live transition). An ``enumerate_active_jobs`` failure is
        tolerated (logged, then the in-memory cache alone is used) so a snapshot
        hiccup never kills the stream — the P8 spirit applied at connect time."""
        async with self._lock:
            self._prune_locked()
            by_id: dict[str, JobEvent] = {
                job_id: event
                for job_id, event in self._latest.items()
                if not event.is_terminal
            }
        if self._enumerate_active_jobs is not None:
            try:
                enumerated = await _maybe_await(self._enumerate_active_jobs())
            except Exception:
                self._log.exception("jobs producer: enumerate_active_jobs failed")
                enumerated = []
            for event in enumerated or []:
                if event.is_terminal:
                    continue
                # In-memory latest wins (freshest); only add engine jobs the hub
                # has not itself seen a live event for.
                by_id.setdefault(event.job_id, event)
        return list(by_id.values())

    async def subscribe(self) -> AsyncIterator[JobEvent]:
        """Yield the active-only snapshot, then live events until disconnect.

        The snapshot replays every **non-terminal** job (``running`` / ``paused``
        / ``queued`` / ``scheduled``) so a late subscriber — or ``jobs``
        reconnecting — immediately learns the full state of in-flight and
        scheduled work. Terminal jobs are never replayed (their terminal event
        already reached earlier subscribers and they live on only in the ``jobs``
        store)."""
        # Register the queue BEFORE building the snapshot: an event published
        # while an async enumerate_active_jobs() is awaited must still reach this
        # subscriber. Registering *after* the snapshot drops such an event — it is
        # in neither the already-captured snapshot nor the not-yet-registered
        # queue. Registering first, it may now appear in BOTH the snapshot and the
        # queue; the consumer's ts-ordering / newest-wins guards absorb the
        # duplicate. The per-service registries this replaces add the queue inside
        # the snapshot lock, so this preserves their no-lost-event guarantee.
        queue: asyncio.Queue[JobEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        try:
            for event in await self._snapshot_active():
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    # -- never-raise NDJSON generator (P8) --------------------------------

    async def stream_ndjson(self) -> AsyncIterator[str]:
        """The producer's ``GET /internal/jobs/v1/stream`` body — a never-raise
        NDJSON generator.

        Yields ``JobEvent.to_json_line()`` for the snapshot then live events, and
        — once the response has started — **catches all exceptions** rather than
        propagating them (P8): a raise here would reset the connection and surface
        as a generic network error to the ``jobs`` subscriber, which then simply
        reconnects. On any error it logs and returns cleanly, ending the stream.
        """
        try:
            async for event in self.subscribe():
                yield event.to_json_line()
        except Exception:  # noqa: BLE001 — P8: never raise out of a started stream
            self._log.exception("jobs producer: stream terminated early")
            return

    # -- control-dispatch scaffold ----------------------------------------

    async def dispatch_control(
        self, job_id: str, action: JobControlAction | str
    ) -> JobEvent | None:
        """Apply a control action to the engine and re-publish its outcome.

        Coerces ``action`` to a :class:`~fm_runtime.job_events.JobControlAction`
        (raising ``ValueError`` on an unknown action — a producer route should map
        that to a 400), calls the registered ``apply_control(job_id, action)``,
        and — if it returns a ``JobEvent`` — **publishes** it so the stream stays
        authoritative for the control outcome. Returns the resulting event (or
        ``None`` when the engine reports nothing to do / an unknown job).

        Raises :class:`RuntimeError` if the producer was built without an
        ``apply_control`` callback — a producer offering the control route must
        supply one."""
        control = JobControlAction.coerce(action)
        if self._apply_control is None:
            raise RuntimeError(
                "JobProducer.dispatch_control called without an apply_control "
                "callback; supply one at construction to offer job control."
            )
        result = await _maybe_await(self._apply_control(job_id, control))
        if result is not None:
            if not isinstance(result, JobEvent):
                raise TypeError(
                    "apply_control must return a JobEvent or None, got "
                    f"{type(result)!r}"
                )
            await self.publish(result)
        return result


__all__ = [
    "DEFAULT_TERMINAL_RETENTION_SECONDS",
    "ApplyControl",
    "EnumerateActiveJobs",
    "JobProducer",
]


# --------------------------------------------------------------------------
# Inline self-check (P11 floor / wave-1 verify): run ``python -m
# fm_runtime.jobs_producer`` to exercise the hub end-to-end. Demonstrates:
#   1. publish -> a LATE subscriber still receives the active (non-terminal)
#      snapshot, now including a SCHEDULED job;
#   2. terminal jobs prune out of the snapshot;
#   3. the never-raise NDJSON generator does not propagate even when an engine
#      callback (enumerate_active_jobs) raises.
# Not a substitute for the wave-1 pytest suite; a fast smoke that the module's
# core contract holds.
# --------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from fm_runtime.job_events import JobStatus

    async def _selfcheck() -> None:
        # 1. Late subscriber gets the active snapshot (running + scheduled),
        #    not the terminal one.
        hub = JobProducer(terminal_retention_seconds=1000.0)
        await hub.publish(
            JobEvent(job_id="run-1", type="agent_turn", user="alice",
                     status=JobStatus.RUNNING, progress=0.5)
        )
        await hub.publish(
            JobEvent(job_id="sched-1", type="agent_schedule", user="alice",
                     status=JobStatus.SCHEDULED)
        )
        await hub.publish(
            JobEvent(job_id="done-1", type="agent_turn", user="alice",
                     status=JobStatus.COMPLETED, exit_status="ok")
        )
        snap = await hub._snapshot_active()
        ids = {e.job_id for e in snap}
        assert ids == {"run-1", "sched-1"}, f"unexpected snapshot: {ids}"
        assert not any(e.is_terminal for e in snap)
        sched = next(e for e in snap if e.job_id == "sched-1")
        assert sched.status is JobStatus.SCHEDULED and not sched.is_terminal
        print("[1] late-subscriber active snapshot (incl SCHEDULED): OK", ids)

        # 2. Terminal prune: force retention to 0 and confirm terminal drops.
        hub_prune = JobProducer(terminal_retention_seconds=0.0)
        await hub_prune.publish(
            JobEvent(job_id="a", type="t", user="u", status=JobStatus.RUNNING)
        )
        await hub_prune.publish(
            JobEvent(job_id="b", type="t", user="u", status=JobStatus.COMPLETED)
        )
        # A subsequent publish triggers _prune_locked, evicting the terminal job.
        await hub_prune.publish(
            JobEvent(job_id="a", type="t", user="u", status=JobStatus.RUNNING,
                     progress=0.9)
        )
        snap2 = await hub_prune._snapshot_active()
        assert {e.job_id for e in snap2} == {"a"}, snap2
        print("[2] terminal prune: OK")

        # 3. never-raise NDJSON generator with a raising engine callback.
        def _boom() -> list[JobEvent]:
            raise RuntimeError("engine enumerate blew up")

        hub_raise = JobProducer(enumerate_active_jobs=_boom)
        await hub_raise.publish(
            JobEvent(job_id="live-1", type="t", user="u", status=JobStatus.RUNNING)
        )
        lines: list[str] = []
        gen = hub_raise.stream_ndjson()
        # Drain the snapshot portion (enumerate raised but was tolerated, so the
        # in-memory cache still yields), then break out of the live loop.
        try:
            async with asyncio.timeout(1.0):
                async for line in gen:
                    lines.append(line)
                    break  # got the snapshot line; stop before the live wait
        finally:
            await gen.aclose()
        assert lines and lines[0].endswith("\n"), lines
        assert '"live-1"' in lines[0], lines
        print("[3] never-raise stream (engine callback raised): OK", len(lines), "line(s)")

        # 3b. Directly prove the generator swallows a mid-stream error: monkeypatch
        #     subscribe to raise, confirm stream_ndjson returns without raising.
        async def _raising_subscribe() -> AsyncIterator[JobEvent]:
            raise RuntimeError("subscribe blew up")
            yield  # pragma: no cover

        hub_raise.subscribe = _raising_subscribe  # type: ignore[method-assign]
        out = [line async for line in hub_raise.stream_ndjson()]
        assert out == [], out
        print("[3b] never-raise stream (subscribe raised): OK (empty, no raise)")

        print("jobs_producer self-check: ALL OK")

    asyncio.run(_selfcheck())
