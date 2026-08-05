"""Producer side of the jobs contract — ``/internal/jobs/v1/*`` (agents).

The ``agents`` service is a **v1 job producer**: each runtime-agent run is a job.
It publishes lifecycle ``JobEvent``s on the stream and exposes control so the
``jobs`` service can pause/resume/cancel a run.

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
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from fm_runtime import JobControlAction, Principal, require_principal

from app.jobs_registry import job_registry
from app.runner import turn_runner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/jobs/v1", tags=["internal-jobs"])


@router.get("/stream")
async def jobs_stream(
    _: Principal = Depends(require_principal),
) -> StreamingResponse:
    """NDJSON stream of ``JobEvent``s for agent runs (jobs service only).

    Replays a snapshot of every still-running run on connect, then streams live
    events. Never raises once the response has started (the jobs subscriber
    reconnects on a clean close)."""

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in job_registry.subscribe():
                yield event.to_json_line()
        except Exception:
            # Never raise out of a started stream — the jobs subscriber reconnects.
            logger.exception("agents jobs stream terminated early")
            return

    return StreamingResponse(
        event_stream(),
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
    """Pause/resume/cancel an agent run (jobs service only). Idempotent; returns
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
        "agents jobs control %s on %s by service=%s acting_user=%s",
        control.value,
        cleaned,
        principal.actor or principal.username,
        acting_user or "-",
    )

    new_status, applied = await turn_runner.control(cleaned, control.value)
    return {
        "job_id": cleaned,
        "action": control.value,
        "status": new_status,
        "applied": applied,
        "acting_user": acting_user or None,
    }
