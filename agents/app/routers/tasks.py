"""Task API — start and browse runtime-AI-agent runs.

``POST /api/agents/tasks`` starts an async run; the GET endpoints list/get runs.
Authorization: the whole ``/api/agents`` surface is gated by the ``agents-access``
realm role (fm_runtime grants / OPA) — the role only decides whether you may call
the API at all. **Within** the service every user sees **every** run (principle 1:
uniform data per user, no owner-only filtering); a run's ``owner``/``origin``/
``actor`` are exposed so the UI can render attribution, but never used to hide a
run from another ``agents-access`` user. Writes are attributed to the initiating
human (``owner = preferred_username``, ``origin = agent``).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fm_runtime import (
    ORIGIN_AGENT,
    JobStatus,
    Principal,
    make_human_approval,
    require_principal,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals import (
    ApprovalDecision,
    approval_coordinator,
    get_pending_approval,
    is_ref_consumed,
    mark_decided,
)
from app.database import get_db
from app.jobs_registry import JobContext, publish_job
from app.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    STATUS_QUEUED,
    AgentRun,
    PendingApproval,
)
from app.runner import agents_actor, run_manager
from app.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CreateTaskRequest,
    PendingApprovalOut,
    TaskDetail,
    TaskListResponse,
    TaskSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Cap a list response so a huge history cannot be pulled in one call.
_MAX_LIMIT = 200


def _to_summary(run: AgentRun) -> TaskSummary:
    return TaskSummary(
        id=run.id,
        goal=run.goal,
        status=run.status,
        owner=run.owner,
        origin=run.origin,
        actor=run.actor,
        progress=run.progress,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
    )


def _pending_out(row: PendingApproval) -> PendingApprovalOut:
    return PendingApprovalOut(
        id=row.id,
        run_id=row.run_id,
        subject=row.subject,
        approval_ref=row.approval_ref,
        action=row.action,
        estimate=row.estimate,
        threshold=row.threshold,
        unit=row.unit,
        message=row.message,
        tool_name=row.tool_name,
        status=row.status,
        created_at=row.created_at,
    )


def _to_detail(
    run: AgentRun, pending: list[PendingApproval] | None = None
) -> TaskDetail:
    return TaskDetail(
        **_to_summary(run).model_dump(),
        params=run.params or {},
        result=run.result,
        error=run.error,
        steps=run.steps,
        usage=run.usage,
        pending_approvals=[_pending_out(p) for p in (pending or [])],
    )


@router.post("/tasks", response_model=TaskDetail, status_code=201)
async def create_task(
    body: CreateTaskRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> TaskDetail:
    """Start a runtime-agent run. The run acts as the initiating human via the
    agents identity (``fm_origin=agent``), exclusively through MCP tools."""
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal must not be empty")

    run_id = uuid.uuid4().hex
    # Attribution: the record owner is the initiating human; a runtime-agent run
    # is ALWAYS origin=agent; actor is the agents service client acting downstream.
    ctx = JobContext(user=principal.username, origin=ORIGIN_AGENT, actor=agents_actor())

    run = AgentRun(
        id=run_id,
        goal=goal,
        params=body.params or {},
        status=STATUS_QUEUED,
        owner=ctx.user,
        origin=ctx.origin,
        actor=ctx.actor,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Publish the queued job BEFORE spawning so a fast run never races ahead of
    # its own creation event on the jobs stream.
    await publish_job(job_id=run_id, ctx=ctx, status=JobStatus.QUEUED, progress=0.0)

    # Capture the human's subject token for the detached run (exchanged per MCP
    # call until it expires, then the run downgrades to the service identity).
    run_manager.start(
        run_id=run_id,
        goal=goal,
        params=body.params or {},
        ctx=ctx,
        subject_token=principal.raw_token or None,
    )

    return _to_detail(run)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
    owner: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TaskListResponse:
    """List runs — cross-user (principle 1). ``owner``/``status`` are optional
    convenience filters (e.g. "show alice's runs"), never a security boundary."""
    limit = max(1, min(_MAX_LIMIT, limit))
    offset = max(0, offset)

    stmt = select(AgentRun)
    if owner:
        stmt = stmt.where(AgentRun.owner == owner)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    stmt = stmt.order_by(AgentRun.created_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).scalars().all()
    return TaskListResponse(tasks=[_to_summary(r) for r in rows])


@router.get("/tasks/{run_id}", response_model=TaskDetail)
async def get_task(
    run_id: str,
    _: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> TaskDetail:
    """Get one run (any user's — principle 1), including any Principle-4 pending
    approvals blocking it (``agentsui`` renders + acts on these)."""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task not found")
    pending = (
        await db.execute(
            select(PendingApproval)
            .where(
                PendingApproval.run_id == run_id,
                PendingApproval.status == APPROVAL_PENDING,
            )
            .order_by(PendingApproval.created_at.asc())
        )
    ).scalars().all()
    return _to_detail(run, list(pending))


@router.post(
    "/tasks/{run_id}/approvals/{approval_id}",
    response_model=ApprovalDecisionResponse,
)
async def decide_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    principal: Principal = Depends(require_principal),
    db: AsyncSession = Depends(get_db),
) -> ApprovalDecisionResponse:
    """Approve or reject a Principle-4 pending approval for a paused run.

    A runtime agent can NEVER self-confirm an over-threshold action; this is the
    only lever that lets one proceed. **Only the initiating human** (the run's
    ``owner``, acting as a real person — never via an agent) may decide. On
    *approve* the ``agents`` service mints an unforgeable ``human_approval`` token
    on that human's behalf, bound to the exact action+estimate+subject, and
    resumes the run so it re-issues the SAME tool call WITH the approval. On
    *reject* the action is skipped and the run continues (or stops) per policy."""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task not found")

    approval = await get_pending_approval(db, approval_id)
    if approval is None or approval.run_id != run_id:
        raise HTTPException(status_code=404, detail="approval not found")

    # --- who may approve (security boundary) ------------------------------
    # Only the initiating human may authorize their own expensive action, and
    # they must be a real person: an agent-origin principal can never approve
    # (that would let a runtime agent self-confirm through a second agent).
    if principal.origin == ORIGIN_AGENT:
        raise HTTPException(
            status_code=403,
            detail="an AI agent cannot approve an expensive action; a human must",
        )
    if principal.username != run.owner:
        raise HTTPException(
            status_code=403,
            detail="only the initiating human may approve or reject this action",
        )

    if approval.status != APPROVAL_PENDING:
        raise HTTPException(
            status_code=409, detail=f"approval already {approval.status}"
        )

    # The run must still be paused awaiting THIS approval in-process; otherwise it
    # ended or moved on and the decision can no longer be applied.
    if not approval_coordinator.is_waiting(approval_id):
        raise HTTPException(
            status_code=409,
            detail="the run is no longer awaiting this approval",
        )

    if body.decision == "approve":
        # Single-use: refuse to authorize an approval_ref already spent by a prior
        # completed action. The human_approval token is replayable within its TTL
        # for the same action+estimate+subject, so a second over-threshold action
        # landing on the same ref must NOT get a fresh (equivalent) token — that
        # would be the replay this whole ledger exists to stop. Resolve the run's
        # waiter as not-approved so it doesn't hang, mark the row, and tell the
        # human why. (The atomic authority is the consumed PK; this is the fast,
        # explicit rejection.)
        if await is_ref_consumed(approval.approval_ref):
            approval_coordinator.resolve(
                approval_id,
                ApprovalDecision(approved=False, decided_by=principal.username),
            )
            await mark_decided(
                approval_id, status=APPROVAL_REJECTED, decided_by=principal.username
            )
            raise HTTPException(
                status_code=409,
                detail="this approval was already used for a completed action and "
                "cannot be reused for another (single-use); the run proceeds "
                "without it",
            )
        # Mint the human-authorized approval token bound to this exact action +
        # estimate + the initiating human's subject. Fail-closed: no configured
        # secret ⇒ refuse (an unsigned token would be forgeable). The token is
        # NEVER returned to the client or the LLM — only injected server-side.
        try:
            token = make_human_approval(
                approval.action, approval.estimate, subject=run.owner
            )
        except RuntimeError:
            logger.error(
                "cannot mint human_approval for run %s: approval secret not configured",
                run_id,
            )
            raise HTTPException(
                status_code=500,
                detail="approval is not configured on this server "
                "(no approval-signing secret); cannot authorize the action",
            ) from None
        delivered = approval_coordinator.resolve(
            approval_id,
            ApprovalDecision(approved=True, token=token, decided_by=principal.username),
        )
        if not delivered:  # lost the race to a cancel/finish
            raise HTTPException(
                status_code=409, detail="the run is no longer awaiting this approval"
            )
        await mark_decided(
            approval_id, status=APPROVAL_APPROVED, decided_by=principal.username
        )
        new_status = APPROVAL_APPROVED
    else:
        delivered = approval_coordinator.resolve(
            approval_id,
            ApprovalDecision(approved=False, decided_by=principal.username),
        )
        if not delivered:
            raise HTTPException(
                status_code=409, detail="the run is no longer awaiting this approval"
            )
        await mark_decided(
            approval_id, status=APPROVAL_REJECTED, decided_by=principal.username
        )
        new_status = APPROVAL_REJECTED

    await db.refresh(approval)
    refreshed_run = await db.get(AgentRun, run_id)
    return ApprovalDecisionResponse(
        approval=_pending_out(approval),
        run_status=refreshed_run.status if refreshed_run else run.status,
        resumed=True,
    )
