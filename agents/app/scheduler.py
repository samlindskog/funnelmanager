"""In-process scheduler for agent schedules (Phase 3) + the internal schedule tool.

Two parts, both single-replica (in-memory poller, like the leads/turn managers):

1. :class:`AgentScheduler` — an ``asyncio`` poll loop, started in the FastAPI
   lifespan. Every ``schedule_poll_interval_seconds`` it reads **due** schedules
   straight from Postgres (so pending schedules are reloaded on boot for free —
   there is no separate in-memory timer to rebuild) and **fires** each by spawning
   a fresh background **turn** on the Phase-2 detached execution path
   (``turn_runner.start_turn``). A one-shot completes; a recurring schedule
   computes its next ``next_run_at`` from cron and stays pending. Because the
   loop is DB-driven, cancelling a schedule (its row flips to ``canceled``) simply
   stops it being selected — no cross-module timer bookkeeping.

2. :func:`build_schedule_tool` — the internal pydantic-ai ``@agent.tool`` a running
   turn calls to schedule future work. It is a **local** tool (not an MCP tool):
   it mutates this service's own DB and needs no new ``mcp->agents`` edge. The
   *expensive* downstream calls a fired turn later makes stay P4-gated at the MCP
   layer exactly as an interactive turn's do — scheduling is cheap, the work it
   schedules is not, and only the latter is gated.

Trace hygiene (LOAD-BEARING): the poll loop runs OUTSIDE any request, so it roots
its own OTel trace; each fired turn additionally roots a fresh trace inside
``runner._execute``. Detached-token posture (item 7): a fired turn reuses the
captured human subject token **only while it is genuinely still valid** — the
``mcp_client`` exchange auth downgrades to the agents service identity
(client-credentials, ``origin=agent``) on genuine expiry, never a transient blip.
The captured token is held **in memory only** (never persisted — a persisted
bearer would be a leak), so after a restart a fired turn simply runs as the
service identity, which is the correct detached-job fallback.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fm_runtime import ORIGIN_AGENT
from fm_runtime import JobStatus as _J
from opentelemetry import context as otel_context

from app import schedules
from app.config import get_settings
from app.cron import cron_is_valid, cron_next
from app.database import SessionLocal
from app.jobs_registry import JobContext, job_producer
from app.models import AgentSession
from app.schedules import schedule_event
from app.session_manager import session_turn_manager

logger = logging.getLogger(__name__)

# Cap on how many schedules a single session may have pending at once — a cheap
# guard so a looping agent can't fill the table with schedules.
_MAX_PENDING_PER_SESSION = 25


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentScheduler:
    """DB-polling scheduler that fires due schedules as background turns."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        # schedule_id -> captured human subject token (IN MEMORY ONLY, never the
        # DB). Present only while the spawning process lives; a fired turn falls
        # back to the service identity once absent/expired (see module docstring).
        self._tokens: dict[str, str] = {}

    # -- captured-token memory (item 7) -----------------------------------

    def remember_token(self, schedule_id: str, token: str | None) -> None:
        if token:
            self._tokens[schedule_id] = token

    def forget_token(self, schedule_id: str) -> None:
        self._tokens.pop(schedule_id, None)

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Start the poll loop (called from the FastAPI lifespan)."""
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agent-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        # Root a fresh OTel trace for this background task so its spans (and DB
        # instrumentation) never attach to a stale/sampled parent — see the
        # module docstring + runner._execute's identical guard for fired turns.
        otel_reset = otel_context.attach(otel_context.Context())
        interval = max(1.0, float(get_settings().schedule_poll_interval_seconds))
        logger.info("agent scheduler started (poll interval %.1fs)", interval)
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 — one bad tick must not kill the loop
                    logger.exception("agent scheduler tick failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            otel_context.detach(otel_reset)
            logger.info("agent scheduler stopped")

    async def _tick(self) -> None:
        """One poll: fire every due schedule. Ticks never overlap (a long tick
        holds the lock; the loop just waits)."""
        async with self._tick_lock:
            now = _utcnow()
            for row in await schedules.list_due_schedules(now):
                if self._stop.is_set():
                    return
                try:
                    await self._fire(row, now)
                except Exception:  # noqa: BLE001 — a bad schedule must not stall the rest
                    logger.exception("agent scheduler: failed to fire schedule %s", row.id)

    async def _fire(self, row: Any, now: datetime) -> None:
        """Fire one due schedule: spawn a background turn, then advance/complete
        the schedule and publish its lifecycle onto the jobs stream."""
        recurring = schedules.is_recurring(row.spec)

        # Defer if the session already has a live turn (a chat is serial). Retry on
        # a later tick rather than dropping the firing.
        if session_turn_manager.active_turn_for_session(row.session_id) is not None:
            logger.info(
                "agent scheduler: schedule %s deferred — session %s has a live turn",
                row.id, row.session_id,
            )
            return

        async with SessionLocal() as db:
            session = await db.get(AgentSession, row.session_id)
        if session is None:
            # Orphaned schedule (session gone despite the cascade) — cancel it so
            # it stops firing and terminalizes on the jobs stream.
            cancelled = await schedules.cancel_schedule(row.id)
            self.forget_token(row.id)
            if cancelled is not None:
                await job_producer.publish(
                    schedule_event(cancelled, _J.CANCELED, exit_status="session_deleted")
                )
            return

        turn_id = uuid.uuid4().hex
        try:
            await session_turn_manager.start_turn(row.session_id, turn_id)
        except RuntimeError:
            # Raced a live turn between the check and here — defer to next tick.
            logger.info(
                "agent scheduler: schedule %s deferred — session %s turn race",
                row.id, row.session_id,
            )
            return

        # Claim the firing now that the turn buffer is ours (prevents re-selection
        # on the next tick): advance a recurring schedule / complete a one-shot.
        next_run_at: datetime | None = None
        if recurring:
            next_run_at = cron_next(row.spec["cron"], now)
            await schedules.advance_recurring(row.id, fired_at=now, next_run_at=next_run_at)
        else:
            await schedules.mark_fired_oneshot(row.id, fired_at=now)

        # Reuse the captured human token while it is still valid (mcp_client
        # downgrades on genuine expiry). One-shot: pop (single firing). Recurring:
        # keep until the schedule ends.
        token = self._tokens.pop(row.id, None) if not recurring else self._tokens.get(row.id)

        ctx = JobContext(
            user=row.owner,
            origin=row.origin or ORIGIN_AGENT,
            actor=row.actor or session.actor,
        )

        # A one-shot schedule job goes SCHEDULED -> RUNNING -> COMPLETED around the
        # single firing; a recurring one stays SCHEDULED (each firing spawns its own
        # agent_turn job, which is what the user watches).
        if not recurring:
            await job_producer.publish(schedule_event(row, _J.RUNNING))

        from app.runner import turn_runner  # local: runner imports this module lazily

        turn_runner.start_turn(
            session_id=row.session_id,
            turn_id=turn_id,
            user_message=row.prompt,
            model=session.model,
            ctx=ctx,
            subject_token=token,
        )
        logger.info(
            "agent scheduler: fired schedule %s (%s) -> turn %s for session %s",
            row.id, "recurring" if recurring else "once", turn_id, row.session_id,
        )

        # Republish the schedule's own lifecycle.
        fresh = await schedules.get_schedule(row.id)
        if recurring:
            if next_run_at is not None and fresh is not None:
                await job_producer.publish(schedule_event(fresh, _J.SCHEDULED))
            else:
                # Cron exhausted — the row was completed by advance_recurring.
                self.forget_token(row.id)
                if fresh is not None:
                    await job_producer.publish(
                        schedule_event(fresh, _J.COMPLETED, exit_status="ok")
                    )
        else:
            if fresh is not None:
                await job_producer.publish(schedule_event(fresh, _J.COMPLETED, exit_status="ok"))


agent_scheduler = AgentScheduler()


# --- the internal schedule tool --------------------------------------------


def build_schedule_tool(*, session_id: str, ctx: JobContext, subject_token: str | None):
    """Build the ``schedule_agent_job`` local tool bound to the current turn.

    Registered on the runtime ``Agent`` (``tools=[...]``) alongside the MCP
    toolset. Closes over the initiating session + attribution + the captured human
    token so a schedule it creates is attributed to the same human and can reuse
    their token while still valid. Returns a plain dict result (never raises) so a
    bad argument is reported back to the model to correct, not turned into a
    retryable tool error.
    """

    async def schedule_agent_job(
        prompt: str, at: str | None = None, cron: str | None = None
    ) -> dict[str, Any]:
        """Schedule this agent to run a task LATER, in the background, even after the
        user closes the tab.

        Provide exactly ONE of:
        - ``at``: an ISO-8601 timestamp for a single future run, e.g.
          ``"2026-08-07T14:30:00Z"`` (assumed UTC if it carries no timezone). Must
          be in the future.
        - ``cron``: a standard 5-field cron expression for a recurring run
          (minute hour day-of-month month day-of-week), e.g. ``"0 9 * * 1"`` for
          every Monday at 09:00 UTC.

        ``prompt`` is the instruction the scheduled run will execute (write it as a
        self-contained task; the scheduled run starts a fresh turn in this session).
        When it fires, the scheduled run acts through the same tools you do and any
        expensive action it attempts still requires human approval.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return {"error": "invalid_argument", "message": "prompt must not be empty."}
        has_at = bool(at and at.strip())
        has_cron = bool(cron and cron.strip())
        if has_at == has_cron:
            return {
                "error": "invalid_argument",
                "message": "Provide exactly one of `at` (one-shot ISO timestamp) or "
                "`cron` (recurring 5-field expression).",
            }

        now = _utcnow()
        if has_at:
            raw = at.strip()
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return {
                    "error": "invalid_argument",
                    "message": f"`at` is not a valid ISO-8601 timestamp: {raw!r}.",
                }
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(timezone.utc)
            if when <= now:
                return {
                    "error": "invalid_argument",
                    "message": "`at` must be in the future.",
                }
            spec = {"at": when.isoformat()}
            next_run_at = when
        else:
            expr = cron.strip()
            if not cron_is_valid(expr):
                return {
                    "error": "invalid_argument",
                    "message": f"`cron` is not a valid 5-field cron expression: {expr!r}.",
                }
            next_run_at = cron_next(expr, now)
            if next_run_at is None:
                return {
                    "error": "invalid_argument",
                    "message": f"`cron` {expr!r} has no upcoming run time.",
                }
            spec = {"cron": expr}

        # Cheap anti-abuse cap (a looping agent must not flood the table).
        pending = await schedules.list_scheduled_for_session(session_id)
        if len(pending) >= _MAX_PENDING_PER_SESSION:
            return {
                "error": "limit_reached",
                "message": f"This session already has {len(pending)} pending schedules "
                f"(max {_MAX_PENDING_PER_SESSION}); cancel one before adding another.",
            }

        row = await schedules.create_schedule(
            session_id=session_id,
            owner=ctx.user,
            origin=ctx.origin or ORIGIN_AGENT,
            actor=ctx.actor,
            spec=spec,
            prompt=prompt,
            next_run_at=next_run_at,
        )
        agent_scheduler.remember_token(row.id, subject_token)
        # Surface it on the jobs stream immediately as a pending (SCHEDULED) job.
        await job_producer.publish(schedule_event(row, _J.SCHEDULED))
        logger.info(
            "agent scheduler: session %s scheduled %s job %s at %s",
            session_id,
            "recurring" if has_cron else "once",
            row.id,
            next_run_at.isoformat(),
        )
        return {
            "scheduled": True,
            "schedule_id": row.id,
            "kind": "recurring" if has_cron else "once",
            "next_run_at": next_run_at.isoformat(),
            "message": "Scheduled. It will run in the background and appear as a job; "
            "the user can cancel it from the jobs view.",
        }

    return schedule_agent_job


__all__ = ["AgentScheduler", "agent_scheduler", "build_schedule_tool"]
