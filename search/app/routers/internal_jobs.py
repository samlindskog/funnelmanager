"""Producer side of the jobs contract — ``/internal/jobs/v1/*`` (search).

Search is a **job producer**: it publishes lifecycle ``JobEvent``s for its Apollo
ingests, embedding passes, and semantic searches, and exposes control so the
``jobs`` service can pause/resume/cancel them.

INTERNAL-JOBS TRUST BOUNDARY (binding):
- These routes are callable **only by the ``jobs`` service**, authorized by the
  ``jobs-internal`` realm role held by the jobs client's **service account**
  (client-credentials, ``azp=jobs``). Enforced by fm_runtime grants (compose) /
  OPA (mesh); ``jobs-internal`` is scoped to ``/internal/jobs`` on this producer.
  No human role reaches here, and nginx does not expose ``/internal/*``.
- BOTH the stream read and the control write authenticate as the jobs service
  account — **not** the acting human. The human was already authorized at the
  jobs MCP API; here they ride only as **audit metadata** (``X-FM-Acting-User``).
- The control write maps onto leads' stream control hook using **search's own
  service identity** (``search->leads`` edge), NOT the inbound jobs token —
  ``jobs->leads`` is not an allowed exchange edge, and the human subject may be
  long gone. See ``_leads_control`` below.

Versioned ``/v1`` — additive only within the version. ``JobEvent`` is constructed
strictly per fm_runtime.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from fm_runtime import (
    JobControlAction,
    JobEvent,
    JobStatus,
    Principal,
    require_principal,
)

from app.config import Settings, get_settings
from app.jobs_registry import job_registry, publish_job
from app.leads_client import LeadsClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/jobs/v1", tags=["internal-jobs"])

# leads stream status word -> our JobStatus. leads uses `complete`/`error` /
# `canceled` / `paused` / `running`; map onto the shared enum. Unknown words fall
# back to a safe non-terminal RUNNING (never wedge a job in a status we cannot
# read) — mirrors JobStatus.parse's leniency at this producer<-engine boundary.
_LEADS_STATUS_TO_JOB: dict[str, JobStatus] = {
    "running": JobStatus.RUNNING,
    "paused": JobStatus.PAUSED,
    "canceled": JobStatus.CANCELED,
    "cancelled": JobStatus.CANCELED,
    "complete": JobStatus.COMPLETED,
    "completed": JobStatus.COMPLETED,
    "error": JobStatus.FAILED,
    "failed": JobStatus.FAILED,
}


@router.get("/stream")
async def jobs_stream(
    _: Principal = Depends(require_principal),
) -> StreamingResponse:
    """NDJSON stream of ``JobEvent``s for search's jobs (jobs service only).

    Replays a snapshot of every still-running job on connect, then streams live
    events. Never raises once the response has started (the jobs subscriber
    reconnects on a clean close)."""

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in job_registry.subscribe():
                yield event.to_json_line()
        except Exception:
            # Never raise out of a started stream — the jobs subscriber reconnects.
            logger.exception("jobs stream terminated early")
            return

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _leads_control(settings: Settings, job_id: str, action: str) -> dict:
    """Map a control action onto the leads stream engine using search's OWN
    service identity (client credentials).

    The inbound caller is the jobs service account (``azp=jobs``); ``jobs->leads``
    is NOT an allowed exchange edge, so we must not use its token as the exchange
    subject. ``LeadsClient(settings, token=None)`` acts as the search service
    (``search->leads``, ``internal-service`` grant) — the correct, always-valid
    identity for detached control regardless of whether the human subject expired.
    """
    client = LeadsClient(settings, token=None)
    return await client.stream_control(job_id, action)


@router.post("/{job_id}/{action}")
async def jobs_control(
    job_id: str,
    action: str,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_principal),
    x_fm_acting_user: str | None = Header(default=None),
) -> dict:
    """Pause/resume/cancel a search job (jobs service only). Idempotent; returns
    the new status. ``X-FM-Acting-User`` is recorded for attribution only — the
    call is authorized as the jobs service account, not the human."""
    try:
        control = JobControlAction.coerce(action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cleaned = str(job_id or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="job_id is required")

    acting_user = (x_fm_acting_user or "").strip()
    logger.info(
        "jobs control %s on %s by service=%s acting_user=%s",
        control.value,
        cleaned,
        principal.actor or principal.username,
        acting_user or "-",
    )

    # A semantic search job (`sem-<id>`) has no leads stream — nothing to control.
    if cleaned.startswith("sem-"):
        raise HTTPException(
            status_code=409,
            detail="semantic search jobs are synchronous and cannot be controlled",
        )

    result = await _leads_control(settings, cleaned, control.value)
    leads_status = str(result.get("status") or "").strip().lower()
    new_status = _LEADS_STATUS_TO_JOB.get(leads_status, JobStatus.RUNNING)
    applied = bool(result.get("applied"))

    # Re-publish a JobEvent so the jobs store reflects the control outcome. Reuse
    # the known job's attribution/type when we have it; record the acting human.
    prior = await job_registry.latest(cleaned)
    if prior is not None:
        event = JobEvent(
            job_id=cleaned,
            type=prior.type,
            user=prior.user,
            origin=prior.origin,
            actor=prior.actor,
            status=new_status,
            progress=prior.progress,
            exit_status="ok" if new_status is JobStatus.CANCELED else None,
            meta={**prior.meta, "control": control.value, "acting_user": acting_user},
        )
        await job_registry.publish(event)
    else:
        await publish_job(
            job_id=cleaned,
            job_type="apollo_search",
            ctx=None,  # unknown job (search restarted): control still proxied
            status=new_status,
        )

    return {
        "job_id": cleaned,
        "action": control.value,
        "status": new_status.value,
        "applied": applied,
        "acting_user": acting_user or None,
    }
