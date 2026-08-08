"""Producer side of the jobs contract — ``/internal/jobs/v1/*`` (agents).

The ``agents`` service is a **v1 job producer**: runtime-agent **turns** and
**schedules** are jobs. It publishes lifecycle ``JobEvent``s on the stream and
exposes control so the ``jobs`` service can pause/resume/cancel them. The producer
machinery (broadcast hub, active-only snapshot incl. reloaded schedules, TTL
prune, never-raise NDJSON generator, control apply-then-republish) lives in
:class:`fm_runtime.JobProducer`; the single instance and the engine callbacks are
wired in ``app.jobs_registry``. These two routes are thin adapters onto
``job_producer`` that keep the trust boundary — the sibling of search's producer
router.

INTERNAL-JOBS TRUST BOUNDARY (binding — identical to search's producer side):
- These routes are callable **only by the ``jobs`` service**, authorized by the
  ``jobs-internal`` realm role held by the jobs client's **service account**
  (client-credentials, ``azp=jobs``). Enforced by fm_runtime grants (compose) /
  OPA (mesh); ``jobs-internal`` is scoped to ``/internal/jobs`` on this producer.
  No human role reaches here (the ``agents-access`` grant covers only
  ``/api/agents``), and nginx does not expose ``/internal/*``.
- BOTH the stream read and the control write authenticate as the jobs service
  account — **not** the acting human. The human was already authorized at the
  jobs MCP API; here they ride only as **audit metadata** (``X-FM-Acting-User``).

Versioned ``/v1`` — additive only within the version. ``JobEvent`` is constructed
strictly per fm_runtime (in ``jobs_registry``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from fm_runtime import JobControlAction, Principal, require_principal

from app.jobs_registry import job_producer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/jobs/v1", tags=["internal-jobs"])


@router.get("/stream")
async def jobs_stream(
    _: Principal = Depends(require_principal),
) -> StreamingResponse:
    """NDJSON stream of ``JobEvent``s for agent turns + schedules (jobs service
    only).

    Replays a snapshot of every non-terminal job (running/paused turns +
    scheduled schedules) on connect, then streams live events. Never raises once
    the response has started (the jobs subscriber reconnects on a clean close)."""
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
    """Pause/resume/cancel an agent turn or schedule (jobs service only).
    Idempotent; returns the new status. ``X-FM-Acting-User`` is recorded for
    attribution only — the call is authorized as the jobs service account, not the
    human.

    ``(status, applied)`` are derived directly from the ``JobEvent``
    ``dispatch_control`` returns (an event ⇒ the control applied; ``None`` ⇒ a
    no-op / unknown job) — no request-scoped contextvars needed."""
    try:
        control = JobControlAction.coerce(action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cleaned = str(job_id or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="job_id is required")

    acting_user = (x_fm_acting_user or "").strip()
    logger.info(
        "agents jobs control %s on %s by service=%s acting_user=%s",
        control.value,
        cleaned,
        principal.actor or principal.username,
        acting_user or "-",
    )

    # Apply the action to the agents engine and re-publish the outcome onto the
    # stream (both handled by JobProducer.dispatch_control -> apply_control).
    event = await job_producer.dispatch_control(cleaned, control.value)
    applied = event is not None
    status = event.status.value if event is not None else "completed"
    return {
        "job_id": cleaned,
        "action": control.value,
        "status": status,
        "applied": applied,
        "acting_user": acting_user or None,
    }
