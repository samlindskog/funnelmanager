"""MCP-facing job API — ``/api/jobs/mcp/v1/*``.

These are the endpoints the `mcp` server calls (svc scope ``mcp->jobs``,
audience ``jobs``) to back the ``list_jobs`` / ``get_job`` / ``pause_job`` /
``resume_job`` / ``cancel_job`` tools, so the `agents` service can see what is
running and steer it.

Principle 2 (MCP != UI): these live under a dedicated ``/mcp/v1`` prefix,
DISTINCT from any future UI routes. Any jobs UI would get its own ``/api/jobs/*``
handlers; these are never repurposed for it.

Principle 1 (cross-user visibility): jobs are NEVER filtered by owner. ``user``
is only an OPTIONAL, explicit list filter (attribute, don't hide) — with no
``user`` query param every caller sees every user's jobs. Keycloak's audience +
role is the only access gate.

Versioning: ``/mcp/v1`` — additive within v1; a breaking change ships as
``/mcp/v2``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fm_runtime import JobControlAction, JobStatus, Principal, require_principal
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control import proxy_control
from app.database import get_db
from app.models import Job
from app.schemas import ControlResultOut, JobListOut, JobOut

router = APIRouter(prefix="/api/jobs/mcp/v1", tags=["jobs-mcp"])

# Module-level dependency singletons (FastAPI dependency injection; hoisted so
# the call is not performed inline in a default argument).
_DB = Depends(get_db)
_PRINCIPAL = Depends(require_principal)


@router.get("/jobs", response_model=JobListOut)
async def list_jobs(
    user: str | None = Query(None, description="Filter by human owner (attribution, not access control)"),
    app: str | None = Query(None, description="Filter by producing app"),
    status_filter: str | None = Query(
        None, alias="status", description="Filter by lifecycle status"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = _DB,
    _principal: Principal = _PRINCIPAL,
) -> JobListOut:
    """List tracked jobs, newest activity first. Cross-user by design; the
    filters are optional attributes, never an owner gate."""
    filters = []
    if user is not None:
        filters.append(Job.user == user)
    if app is not None:
        filters.append(Job.app == app)
    if status_filter is not None:
        try:
            filters.append(Job.status == JobStatus.coerce(status_filter).value)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown status {status_filter!r}",
            ) from exc

    total = await db.scalar(select(func.count()).select_from(Job).where(*filters))
    result = await db.execute(
        select(Job)
        .where(*filters)
        # Most recently active first; NULLS LAST so never-eventful rows sink.
        .order_by(Job.last_event_at.desc().nullslast(), Job.id.desc())
        .limit(limit)
        .offset(offset)
    )
    jobs = result.scalars().all()
    return JobListOut(
        jobs=[JobOut.of(job) for job in jobs],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


async def _get_job_or_404(db: AsyncSession, job_id: int) -> Job:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no job with id {job_id}"
        )
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: int,
    db: AsyncSession = _DB,
    _principal: Principal = _PRINCIPAL,
) -> JobOut:
    """Get one job with its current progress/status. Cross-user visible."""
    job = await _get_job_or_404(db, job_id)
    return JobOut.of(job)


@router.post("/jobs/{job_id}/{action}", response_model=ControlResultOut)
async def control_job(
    job_id: int,
    action: str,
    db: AsyncSession = _DB,
    _principal: Principal = _PRINCIPAL,
) -> ControlResultOut:
    """Pause/resume/cancel a job by PROXYING to its owning app's control API.

    Idempotent: re-issuing an action returns the resulting status; controlling a
    job already in a terminal status is a no-op returning that status."""
    try:
        control_action = JobControlAction.coerce(action)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown control action {action!r} (expected pause|resume|cancel)",
        ) from exc

    job = await _get_job_or_404(db, job_id)
    new_status = await proxy_control(db, job, control_action)
    return ControlResultOut(
        id=job.id,
        app=job.app,
        external_job_id=job.external_job_id,
        action=control_action.value,
        status=new_status.value,
    )
