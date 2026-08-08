"""Producer side of the jobs contract — ``/internal/jobs/v1/*`` (search).

Search is a **job producer**: it publishes lifecycle ``JobEvent``s for its Apollo
ingests, embedding passes, and semantic searches, and exposes control so the
``jobs`` service can pause/resume/cancel them. The producer machinery (broadcast
hub, active-only snapshot, TTL prune, never-raise NDJSON generator, control
apply-then-republish) lives in :class:`fm_runtime.JobProducer`; the single
instance and search's engine callback are wired in ``app.jobs_registry``. These
two routes are thin adapters onto ``job_producer`` that keep the trust boundary.

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
  long gone. See ``_leads_control`` / ``_apply_control`` in ``app.jobs_registry``.

Versioned ``/v1`` — additive only within the version. ``JobEvent`` is constructed
strictly per fm_runtime.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from fm_runtime import JobControlAction, JobStatus, Principal, require_principal

from app.jobs_registry import control_request, job_producer, last_control_outcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/jobs/v1", tags=["internal-jobs"])


@router.get("/stream")
async def jobs_stream(
    _: Principal = Depends(require_principal),
) -> StreamingResponse:
    """NDJSON stream of ``JobEvent``s for search's jobs (jobs service only).

    Replays a snapshot of every still-running job on connect, then streams live
    events. Never raises once the response has started (the jobs subscriber
    reconnects on a clean close)."""
    return StreamingResponse(
        job_producer.stream_ndjson(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/{action}")
async def jobs_control(
    job_id: str,
    action: str,
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

    # Apply the action to search's engine and re-publish the outcome onto the
    # stream (both handled by JobProducer.dispatch_control -> apply_control). The
    # (status, applied) outcome + acting-user audit ride via request-scoped
    # contextvars opened by control_request. apply_control raises 409 for a
    # synchronous semantic-search job (no leads stream to control).
    with control_request(acting_user):
        await job_producer.dispatch_control(cleaned, control.value)
        outcome = last_control_outcome()

    new_status, applied = outcome if outcome is not None else (JobStatus.RUNNING, False)

    return {
        "job_id": cleaned,
        "action": control.value,
        "status": new_status.value,
        "applied": applied,
        "acting_user": acting_user or None,
    }
