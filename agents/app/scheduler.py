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
``runner._execute``.

Single-writer + multi-pod safety (fix #1): the captured human token and the
in-process serial-chat guard are **per-pod** state, so the poll loop is designed to
run on exactly ONE pod. It therefore starts only on the primary (non-``canary``)
pod — the agents-canary shares this prod agents-db but must NOT run a competing
scheduler (it exists to trace request flows, not to poll the schedule table). On
top of that, each firing is CLAIMED atomically at the DB level
(``schedules.claim_due_schedule``) BEFORE any turn spawns, as defense-in-depth for
a rollout window where two pollers briefly overlap: exactly one pod wins each
firing, so the atomic claim guarantees no double turn / double spend even then. (A
cross-pod session-serial guarantee for two *different* schedules of one session is
not attempted — that per-process limit is shared with the interactive post path.)

Detached-token / de-auth posture (fix #3 + item 7): a fired turn reuses the
captured human subject token, held **in memory only** (never persisted — a
persisted bearer would be a leak). A scheduled firing must run under the owner's
**live** authorization. If that token is genuinely expired OR absent (e.g. after a
pod restart, when in-memory tokens are gone), the poller does **not** downgrade the
firing to the agents service identity — that would let scheduled work OUTLIVE the
owner's authorization (revoking their ``agents-access`` wouldn't stop it; short-
lived tokens are the de-auth lever). Instead it **pauses** the schedule for re-auth
(``SCHEDULE_PAUSED``, surfaced on the jobs stream) and stops firing it. A paused
schedule is re-armed only when the owner returns and posts to the session — which
they can do only while they still hold ``agents-access`` — so schedule liveness is
tied to the owner's *current* grant. (Within a single already-authorized firing,
``mcp_client`` may still downgrade to the service identity if the fresh token
expires mid-turn; that is a bounded single job, not indefinite scheduled work.)
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
from app.cron import cron_is_valid, cron_min_interval_minutes, cron_next
from app.database import SessionLocal
from app.jobs_registry import JobContext, job_producer
from app.mcp_client import subject_token_expired
from app.models import SCHEDULE_CANCELED, SCHEDULE_SCHEDULED, AgentSession
from app.schedules import is_recurring, schedule_event
from app.session_manager import session_turn_manager

logger = logging.getLogger(__name__)


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
        """Fire one due schedule: atomically CLAIM it (fix #1), then — only if the
        owner's authorization is still live (fix #3) — spawn a background turn and
        publish the schedule's lifecycle onto the jobs stream."""
        recurring = is_recurring(row.spec)

        # (1) Serial-chat guard: a chat is serial, so if THIS pod already has a live
        # turn for the session, defer (retry next tick) rather than claim — the claim
        # is the point of no return and must not be spent on a firing we can't run now.
        if session_turn_manager.active_turn_for_session(row.session_id) is not None:
            logger.info(
                "agent scheduler: schedule %s deferred — session %s has a live turn",
                row.id, row.session_id,
            )
            return

        # (2) AUTHORIZATION GATE (fix #3, P6): a scheduled firing must run under the
        # owner's LIVE authorization, proxied by the captured human subject token.
        # Absent (e.g. after a restart) or genuinely expired ⇒ do NOT fall back to the
        # service identity (that would let scheduled work OUTLIVE the owner's
        # authorization) — pause for re-auth and stop firing. Checked BEFORE the claim
        # so a paused row is left un-advanced.
        if subject_token_expired(self._tokens.get(row.id)):
            await self._pause_for_reauth(row)
            return

        # (3) Compute the recurring next fire up front so the claim commits the exact
        # transition atomically.
        next_run_at: datetime | None = cron_next(row.spec["cron"], now) if recurring else None

        # (4) ATOMIC CLAIM (multi-pod safe): exactly one pod wins this firing; a pod
        # that lost the race — or a schedule cancelled between select and here — gets
        # None and returns.
        claimed = await schedules.claim_due_schedule(
            row.id, now=now, recurring=recurring, next_run_at=next_run_at
        )
        if claimed is None:
            logger.debug(
                "agent scheduler: schedule %s not claimed (lost race / cancelled) — skip",
                row.id,
            )
            return

        # (5) Won the claim. Load the session; if it vanished (raced a delete),
        # terminalize cleanly — cancel the DB row too (not just the jobs event), so a
        # recurring row the claim just re-advanced can't keep becoming due if the
        # cascade ever failed to remove it.
        async with SessionLocal() as db:
            session = await db.get(AgentSession, row.session_id)
        if session is None:
            self.forget_token(row.id)
            canceled = await schedules.cancel_schedule(row.id)
            await job_producer.publish(
                schedule_event(canceled or claimed, _J.CANCELED, exit_status="session_deleted")
            )
            return

        # (6) Reserve the in-process turn buffer + spawn the background turn.
        turn_id = uuid.uuid4().hex
        try:
            await session_turn_manager.start_turn(row.session_id, turn_id)
        except RuntimeError:
            # A live turn raced onto this session on THIS pod between (1) and here.
            # The firing is already claimed (committed): a recurring schedule fires its
            # next occurrence, a one-shot is spent. Republish lifecycle so jobs stays
            # consistent, then skip — never double-run.
            logger.warning(
                "agent scheduler: schedule %s claimed but session %s gained a live "
                "turn; skipping this firing", row.id, row.session_id,
            )
            await self._republish_after_claim(claimed)
            return

        # Reuse the (validated-live) captured human token. One-shot: pop (single
        # firing). Recurring: keep until the schedule ends.
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
            await job_producer.publish(schedule_event(claimed, _J.RUNNING))

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

        # (7) Republish the schedule's own lifecycle (advanced / completed / canceled).
        await self._republish_after_claim(claimed)

    async def _republish_after_claim(self, claimed: Any) -> None:
        """Republish a claimed schedule's lifecycle after firing, from its FRESH
        re-read status (``claim_due_schedule`` re-reads post-commit):
          * ``scheduled`` — a recurring row advanced to its next fire → SCHEDULED;
          * ``canceled`` — a cancel raced into the claim→re-read window → CANCELED
            (NOT COMPLETED — else the jobs stream would tell the user their just-
            canceled schedule "completed successfully");
          * else (``completed``) — a one-shot, or a recurring cron that just
            exhausted → COMPLETED.
        A terminal row drops its captured token."""
        if claimed.status == SCHEDULE_SCHEDULED:
            await job_producer.publish(schedule_event(claimed, _J.SCHEDULED))
        elif claimed.status == SCHEDULE_CANCELED:
            self.forget_token(claimed.id)
            await job_producer.publish(
                schedule_event(claimed, _J.CANCELED, exit_status="canceled")
            )
        else:
            self.forget_token(claimed.id)
            await job_producer.publish(
                schedule_event(claimed, _J.COMPLETED, exit_status="ok")
            )

    async def _pause_for_reauth(self, row: Any) -> None:
        """P6 de-auth (fix #3): the owner's authorization can't be proven for this
        firing (captured token absent/expired). Pause the schedule so the DB-polling
        select stops firing it, drop the (dead) token, and surface it on the jobs
        stream as needing re-auth. Re-armed only by the owner's next post."""
        paused = await schedules.pause_for_reauth(row.id)
        self.forget_token(row.id)
        if paused is None:
            return
        logger.info(
            "agent scheduler: schedule %s paused for re-auth (session %s) — owner "
            "token expired/absent; re-arms when the owner returns to the session",
            row.id, row.session_id,
        )
        await job_producer.publish(
            schedule_event(paused, _J.PAUSED, extra_meta={"needs_reauth": True})
        )

    async def rearm_paused_for_session(self, session_id: str, token: str | None) -> None:
        """Re-arm a session's re-auth-paused schedules with a fresh owner token
        (fix #3). Called when the owner posts a message — which they can do only while
        they still hold ``agents-access`` — so re-arming is implicitly gated by the
        owner's CURRENT authorization. Adds one indexed lookup (session_id + paused
        status) before the turn stream starts; a failure is swallowed so it can never
        fail the post, but the (fast) lookup is awaited inline on purpose — a paused
        schedule must be re-armed deterministically before the response returns, not
        on a floating task that a shutdown could drop."""
        if not token:
            return
        try:
            rearmed = await schedules.rearm_paused(session_id, _utcnow())
        except Exception:  # noqa: BLE001 — re-arm must never fail a message post
            logger.exception(
                "agent scheduler: failed to re-arm schedules for session %s", session_id
            )
            return
        # Remember every token in ONE synchronous pass (no await between) BEFORE the
        # first publish yields to the event loop — so a concurrent poller tick can
        # never observe a re-armed (scheduled) row that has no token yet and re-pause
        # it. (Recurring rows also get a future next_run_at, so only a one-shot's
        # next_run_at=now could even be due; this closes that window entirely.)
        for row in rearmed:
            self.remember_token(row.id, token)
        for row in rearmed:
            await job_producer.publish(schedule_event(row, _J.SCHEDULED))
        if rearmed:
            logger.info(
                "agent scheduler: re-armed %d paused schedule(s) for session %s",
                len(rearmed), session_id,
            )


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
        settings = get_settings()
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
            # P4 Denial-of-Wallet floor (fix #2): reject a cron that can fire more
            # often than the configured minimum recurring interval. Each recurring
            # firing spins up an LLM turn + expensive MCP tool calls, so a
            # prompt-injected `* * * * *` (the agent reasons over untrusted
            # Apollo/Gmail content) would otherwise plant a self-perpetuating turn
            # every minute forever, reloaded from Postgres on every restart. A row
            # cap bounds COUNT, not RATE — this bounds rate.
            floor = settings.schedule_min_recurring_interval_minutes
            tightest = cron_min_interval_minutes(expr, after=now)
            if tightest is not None and tightest < floor:
                return {
                    "error": "invalid_argument",
                    "message": f"That schedule can fire as often as every {tightest:g} "
                    f"minute(s); the minimum recurring interval is {floor} minutes. "
                    f"Use a less frequent cron (e.g. '*/{floor} * * * *' or '0 * * * *').",
                }
            next_run_at = cron_next(expr, now)
            if next_run_at is None:
                return {
                    "error": "invalid_argument",
                    "message": f"`cron` {expr!r} has no upcoming run time.",
                }
            spec = {"cron": expr}

        # Anti-abuse caps (fix #2): a total cap so a looping agent can't flood the
        # table with cheap one-shots, plus a tighter cap on ACTIVE RECURRING schedules
        # per session (a recurring row is a standing spend source, so bound how many
        # can run at once — complements the per-cron min-interval floor). Counts
        # `scheduled` AND `paused` rows: a paused (re-auth) row still holds a slot, so
        # counting only `scheduled` would let a create -> let-token-lapse -> create
        # cycle grow the table unbounded.
        pending = await schedules.list_active_for_session(session_id)
        if len(pending) >= settings.schedule_max_pending_per_session:
            return {
                "error": "limit_reached",
                "message": f"This session already has {len(pending)} pending schedules "
                f"(max {settings.schedule_max_pending_per_session}); cancel one before "
                "adding another.",
            }
        if has_cron:
            recurring_count = sum(1 for p in pending if is_recurring(p.spec))
            if recurring_count >= settings.schedule_max_recurring_per_session:
                return {
                    "error": "limit_reached",
                    "message": f"This session already has {recurring_count} active "
                    f"recurring schedules (max "
                    f"{settings.schedule_max_recurring_per_session}); cancel one before "
                    "adding another.",
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
