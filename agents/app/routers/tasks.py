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

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fm_runtime import ORIGIN_AGENT, Principal, require_principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.jobs_registry import JobContext, publish_job
from app.models import STATUS_QUEUED, AgentRun
from app.runner import agents_actor, run_manager
from app.schemas import (
    CreateTaskRequest,
    TaskDetail,
    TaskListResponse,
    TaskSummary,
)

from fm_runtime import JobStatus

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


def _to_detail(run: AgentRun) -> TaskDetail:
    return TaskDetail(
        **_to_summary(run).model_dump(),
        params=run.params or {},
        result=run.result,
        error=run.error,
        steps=run.steps,
        usage=run.usage,
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
    """Get one run (any user's — principle 1)."""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _to_detail(run)
