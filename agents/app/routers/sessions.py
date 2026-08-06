"""Sessions API — interactive, multi-turn runtime-AI-agent chat.

Authorization: the whole ``/api/agents`` surface is gated by the ``agents-access``
realm role (fm_runtime grants / OPA) — the role decides whether you may call the
API at all. **Within** the service every user sees **every** session (Principle 1:
uniform data, no owner-only read filtering); a session's ``owner``/``origin``/
``actor`` are exposed for attribution, never to hide a session. The ONE sanctioned
per-user carve-out is *destructive ownership* — rename/delete of your own session
may 404 another user's row (P1's explicit exception, not a read filter).

Streaming (P8): once an NDJSON turn response has started it NEVER raises — errors
are emitted as ``{"type":"error",...}`` stream lines with a terminal sentinel.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from fm_runtime import (
    ORIGIN_AGENT,
    JobStatus,
    Principal,
    make_human_approval,
    require_principal,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals import (
    ApprovalDecision,
    approval_coordinator,
    get_pending_approval,
    is_ref_consumed,
    mark_decided,
)
from app.config import get_settings
from app.database import get_db
from app.jobs_registry import JobContext
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    MSG_KIND_USER,
    SCHEDULE_SCHEDULED,
    SESSION_STATUS_ERROR,
    SESSION_STATUS_IDLE,
    SESSION_STATUS_PAUSED,
    SESSION_STATUS_RUNNING,
    SESSION_STATUS_SCHEDULED,
    AgentMessage,
    AgentSchedule,
    AgentSession,
    PendingApproval,
)
from app import schedules as schedules_store
from app.models_catalog import estimate_cost, list_models
from app.runner import agents_actor, turn_runner
from app.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CreateSessionRequest,
    MessageOut,
    ModelInfo,
    ModelsResponse,
    ModelUsageStat,
    PendingApprovalOut,
    PostMessageRequest,
    RenameSessionRequest,
    ScheduleOut,
    SessionDetail,
    SessionListResponse,
    SessionSummary,
    StatsResponse,
)
from app.session_manager import TurnStatus, session_turn_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

_MAX_LIMIT = 200


# --- helpers ---------------------------------------------------------------


def _derive_status(session: AgentSession, has_scheduled: bool) -> str:
    """Derive a session's display status from live turn state + persisted signals
    (never stored, so it can't drift)."""
    live = session_turn_manager.active_status(session.id)
    if live is TurnStatus.RUNNING:
        return SESSION_STATUS_RUNNING
    if live is TurnStatus.PAUSED:
        return SESSION_STATUS_PAUSED
    if has_scheduled:
        return SESSION_STATUS_SCHEDULED
    if session.last_error:
        return SESSION_STATUS_ERROR
    return SESSION_STATUS_IDLE


def _summary(session: AgentSession, *, has_scheduled: bool = False) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        title=session.title,
        model=session.model,
        owner=session.owner,
        origin=session.origin,
        actor=session.actor,
        status=_derive_status(session, has_scheduled),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _message_out(row: AgentMessage) -> MessageOut:
    return MessageOut(
        id=row.id, seq=row.seq, turn_id=row.turn_id, kind=row.kind,
        content=row.content or {}, model=row.model, usage=row.usage,
        created_at=row.created_at,
    )


def _pending_out(row: PendingApproval) -> PendingApprovalOut:
    return PendingApprovalOut(
        id=row.id, session_id=row.session_id, turn_id=row.turn_id, subject=row.subject,
        approval_ref=row.approval_ref, action=row.action, estimate=row.estimate,
        threshold=row.threshold, unit=row.unit, message=row.message,
        tool_name=row.tool_name, status=row.status, created_at=row.created_at,
    )


def _schedule_out(row: AgentSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=row.id, session_id=row.session_id, owner=row.owner, origin=row.origin,
        actor=row.actor, spec=row.spec or {}, prompt=row.prompt, status=row.status,
        next_run_at=row.next_run_at, last_run_at=row.last_run_at,
        created_at=row.created_at,
    )


async def _scheduled_session_ids(db: AsyncSession) -> set[str]:
    rows = (
        await db.execute(
            select(AgentSchedule.session_id).where(
                AgentSchedule.status == SCHEDULE_SCHEDULED
            )
        )
    ).scalars().all()
    return set(rows)


# --- sessions CRUD ---------------------------------------------------------


@router.post("/sessions", response_model=SessionDetail, status_code=201)
async def create_session(
    body: CreateSessionRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    """Create a chat session, attributed to the initiating human (owner), with
    ``origin=agent`` (runtime-agent work) and the agents client as ``actor``."""
    settings = get_settings()
    session = AgentSession(
        id=uuid.uuid4().hex,
        owner=principal.username,
        origin=ORIGIN_AGENT,
        actor=agents_actor(),
        title=(body.title or "New session").strip()[:400] or "New session",
        model=(body.model or "").strip()[:128] or settings.model_name,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionDetail(**_summary(session).model_dump(), context_watermark=0,
                         last_error=None, messages=[], pending_approvals=[])


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
    owner: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SessionListResponse:
    """List sessions — cross-user (Principle 1). ``owner`` is an optional
    convenience filter (cross-user *viewing*), never a security boundary."""
    limit = max(1, min(_MAX_LIMIT, limit))
    offset = max(0, offset)

    stmt = select(AgentSession)
    if owner:
        stmt = stmt.where(AgentSession.owner == owner)
    stmt = stmt.order_by(AgentSession.updated_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()

    scheduled = await _scheduled_session_ids(db)
    # Distinct owners across ALL sessions for the cross-user picker.
    owners = (
        await db.execute(select(AgentSession.owner).distinct().order_by(AgentSession.owner))
    ).scalars().all()

    return SessionListResponse(
        sessions=[_summary(s, has_scheduled=s.id in scheduled) for s in rows],
        owners=list(owners),
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    """Full transcript (from ``agent_message``) + any pending Principle-4
    approvals. Cross-user visible (Principle 1)."""
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    messages = (
        await db.execute(
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.seq.asc())
        )
    ).scalars().all()
    pending = (
        await db.execute(
            select(PendingApproval)
            .where(PendingApproval.session_id == session_id,
                   PendingApproval.status == APPROVAL_PENDING)
            .order_by(PendingApproval.created_at.asc())
        )
    ).scalars().all()
    session_schedules = (
        await db.execute(
            select(AgentSchedule)
            .where(AgentSchedule.session_id == session_id,
                   AgentSchedule.status == SCHEDULE_SCHEDULED)
            .order_by(AgentSchedule.created_at.asc())
        )
    ).scalars().all()

    return SessionDetail(
        **_summary(session, has_scheduled=bool(session_schedules)).model_dump(),
        context_watermark=session.context_watermark,
        last_error=session.last_error,
        messages=[_message_out(m) for m in messages],
        pending_approvals=[_pending_out(p) for p in pending],
        schedules=[_schedule_out(s) for s in session_schedules],
    )


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> SessionSummary:
    """Rename a session. Destructive-ownership carve-out (P1): only the owner may
    mutate their own session; another user's row 404s (not a read filter)."""
    session = await db.get(AgentSession, session_id)
    if session is None or session.owner != principal.username:
        raise HTTPException(status_code=404, detail="session not found")
    session.title = body.title.strip()[:400] or session.title
    await db.commit()
    await db.refresh(session)
    return _summary(session)


@router.delete("/sessions/{session_id}", status_code=204, response_class=Response)
async def delete_session(
    session_id: str,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a session (cascades messages/approvals/schedules). Destructive
    ownership (P1): only the owner may delete; another user's row 404s."""
    session = await db.get(AgentSession, session_id)
    if session is None or session.owner != principal.username:
        raise HTTPException(status_code=404, detail="session not found")
    # Cancel any in-flight turn so a detached task isn't orphaned against a
    # deleted session.
    active = session_turn_manager.active_turn_for_session(session_id)
    if active is not None:
        await turn_runner.control(active, "cancel")

    # Terminalize any pending schedules on the jobs stream before the cascade
    # delete removes their rows, so the jobs service doesn't keep showing a
    # scheduled job for a deleted session (the DB-polling scheduler would already
    # stop firing them once the rows are gone).
    from app.jobs_registry import job_producer
    from app.scheduler import agent_scheduler

    pending_schedules = (
        await db.execute(
            select(AgentSchedule).where(
                AgentSchedule.session_id == session_id,
                AgentSchedule.status == SCHEDULE_SCHEDULED,
            )
        )
    ).scalars().all()
    for sched in pending_schedules:
        agent_scheduler.forget_token(sched.id)
        await job_producer.publish(
            schedules_store.schedule_event(sched, JobStatus.CANCELED, exit_status="session_deleted")
        )

    await db.delete(session)
    await db.commit()
    return Response(status_code=204)


# --- turns / streaming -----------------------------------------------------


def _ndjson_response(turn_id: str) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        try:
            async for event in session_turn_manager.subscribe(turn_id):
                yield json.dumps(event) + "\n"
        except Exception:  # noqa: BLE001 — P8: never raise out of a started stream
            logger.exception("agents: session turn stream %s terminated early", turn_id)
            yield json.dumps({"type": "error", "detail": "stream terminated"}) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str,
    body: PostMessageRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Send a user message; stream the resulting turn as NDJSON. Rejects a second
    concurrent turn for the same session with 409 (a chat is serial)."""
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Posting into a session is owner-only (a sanctioned destructive-ownership
    # gate, like rename/delete — NOT a reads filter; cross-user *viewing* stays
    # open). This keeps the session single-actor, which the approval flow already
    # assumes: decide_approval lets only the session owner approve and binds the
    # human_approval token to `session.owner`, while a turn runs under the
    # poster's subject_token — so a non-owner poster whose turn hit the P4 gate
    # would mint a token bound to the wrong subject and be rejected (fails closed).
    # Attribution also stays honest: every message in a session is the owner's.
    if principal.username != session.owner:
        raise HTTPException(
            status_code=403, detail="only the session owner may post to this session"
        )

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="message must not be empty")

    # Auto-title from the first user message (only if still the default).
    existing = (
        await db.execute(
            select(func.count()).select_from(AgentMessage).where(
                AgentMessage.session_id == session_id,
                AgentMessage.kind == MSG_KIND_USER,
            )
        )
    ).scalar() or 0
    if existing == 0 and session.title in ("", "New session"):
        session.title = content[:80]
        await db.commit()

    turn_id = uuid.uuid4().hex
    try:
        await session_turn_manager.start_turn(session_id, turn_id)
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="a turn is already running for this session"
        ) from None

    ctx = JobContext(user=principal.username, origin=ORIGIN_AGENT, actor=agents_actor())
    turn_runner.start_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_message=content,
        model=session.model,
        ctx=ctx,
        subject_token=principal.raw_token or None,
    )
    return _ndjson_response(turn_id)


@router.get("/sessions/{session_id}/stream")
async def reattach_stream(
    session_id: str,
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Reattach to a session's most-recent turn: replay its buffer, then live. This
    resolves a turn that is still running OR one that finished within the ~90s grace
    window (so a client reconnecting just after completion still replays the tail —
    final text, tool results, usage, or the record of a failure). Emits a single
    ``idle`` line only when no turn is buffered for the session."""
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    turn_id = session_turn_manager.latest_turn_for_session(session_id)
    if turn_id is None:
        async def idle() -> AsyncIterator[str]:
            yield json.dumps({"type": "idle"}) + "\n"

        return StreamingResponse(idle(), media_type="application/x-ndjson")
    return _ndjson_response(turn_id)


# --- Principle-4 approvals --------------------------------------------------


@router.post(
    "/sessions/{session_id}/approvals/{approval_id}",
    response_model=ApprovalDecisionResponse,
)
async def decide_approval(
    session_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> ApprovalDecisionResponse:
    """Approve or reject a Principle-4 pending approval for a paused turn.

    A runtime agent can NEVER self-confirm; this is the only lever that lets an
    over-threshold action proceed. Only the initiating human (the session
    ``owner``, acting as a real person — never via an agent) may decide. On
    *approve* the service mints an unforgeable ``human_approval`` token bound to
    the exact action+estimate+subject and resumes the turn so it re-issues the
    SAME tool call WITH the approval. On *reject* the action is skipped."""
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    approval = await get_pending_approval(db, approval_id)
    if approval is None or approval.session_id != session_id:
        raise HTTPException(status_code=404, detail="approval not found")

    # Who may approve (security boundary): only the initiating human, acting as a
    # real person. An agent-origin principal can never approve (that would let a
    # runtime agent self-confirm through a second agent). This owner check is
    # legitimate business state, not authz plumbing — keep it.
    if principal.origin == ORIGIN_AGENT:
        raise HTTPException(
            status_code=403,
            detail="an AI agent cannot approve an expensive action; a human must",
        )
    if principal.username != session.owner:
        raise HTTPException(
            status_code=403,
            detail="only the initiating human may approve or reject this action",
        )
    if approval.status != APPROVAL_PENDING:
        raise HTTPException(status_code=409, detail=f"approval already {approval.status}")
    if not approval_coordinator.is_waiting(approval_id):
        raise HTTPException(
            status_code=409, detail="the turn is no longer awaiting this approval"
        )

    if body.decision == "approve":
        # Single-use: refuse to authorize an approval_ref already spent.
        if await is_ref_consumed(approval.approval_ref):
            approval_coordinator.resolve(
                approval_id, ApprovalDecision(approved=False, decided_by=principal.username)
            )
            await mark_decided(approval_id, status=APPROVAL_REJECTED, decided_by=principal.username)
            raise HTTPException(
                status_code=409,
                detail="this approval was already used for a completed action and "
                "cannot be reused for another (single-use); the turn proceeds without it",
            )
        # Mint the human-authorized token (bound to action+estimate+subject).
        # Fail-closed: no configured secret ⇒ refuse. NEVER returned to a client/LLM.
        try:
            token = make_human_approval(approval.action, approval.estimate, subject=session.owner)
        except RuntimeError:
            logger.error(
                "cannot mint human_approval for session %s: approval secret not configured",
                session_id,
            )
            raise HTTPException(
                status_code=500,
                detail="approval is not configured on this server (no approval-signing "
                "secret); cannot authorize the action",
            ) from None
        delivered = approval_coordinator.resolve(
            approval_id,
            ApprovalDecision(approved=True, token=token, decided_by=principal.username),
        )
        if not delivered:
            raise HTTPException(
                status_code=409, detail="the turn is no longer awaiting this approval"
            )
        await mark_decided(approval_id, status=APPROVAL_APPROVED, decided_by=principal.username)
    else:
        delivered = approval_coordinator.resolve(
            approval_id, ApprovalDecision(approved=False, decided_by=principal.username)
        )
        if not delivered:
            raise HTTPException(
                status_code=409, detail="the turn is no longer awaiting this approval"
            )
        await mark_decided(approval_id, status=APPROVAL_REJECTED, decided_by=principal.username)

    await db.refresh(approval)
    return ApprovalDecisionResponse(approval=_pending_out(approval), resumed=True)


# --- models + stats --------------------------------------------------------


@router.get("/models", response_model=ModelsResponse)
async def get_models(_: Principal = Depends(require_principal)) -> ModelsResponse:
    """Chat-capable OpenAI models joined with the maintained context-window map."""
    settings = get_settings()
    models = await list_models(settings)
    return ModelsResponse(
        models=[ModelInfo(**m) for m in models],
        default=settings.model_name,
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
    owner: str | None = None,
) -> StatsResponse:
    """Aggregate stored per-turn usage grouped by model, with cost via genai-prices.

    Cross-user by default (Principle 1); ``owner`` is an optional viewing filter."""
    stmt = (
        select(AgentMessage, AgentSession.owner)
        .join(AgentSession, AgentSession.id == AgentMessage.session_id)
        .where(AgentMessage.usage.isnot(None))
    )
    if owner:
        stmt = stmt.where(AgentSession.owner == owner)
    rows = (await db.execute(stmt)).all()

    agg: dict[str, dict] = {}
    for message, _owner in rows:
        usage = message.usage or {}
        model = message.model or "unknown"
        bucket = agg.setdefault(
            model,
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "requests": 0, "tool_calls": 0, "turns": 0},
        )
        bucket["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        bucket["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        bucket["requests"] += int(usage.get("requests", 0) or 0)
        bucket["tool_calls"] += int(usage.get("tool_calls", 0) or 0)
        bucket["turns"] += 1

    by_model: list[ModelUsageStat] = []
    total_cost = 0.0
    any_cost = False
    for model, b in sorted(agg.items()):
        cost = estimate_cost(model, b["input_tokens"], b["output_tokens"])
        if cost is not None:
            total_cost += cost
            any_cost = True
        by_model.append(ModelUsageStat(model=model, cost=cost, **b))

    return StatsResponse(by_model=by_model, total_cost=total_cost if any_cost else None)
