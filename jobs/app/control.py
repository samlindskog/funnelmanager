"""Job-control proxy.

The `jobs` service does not run work, so it cannot pause/resume/cancel a job
itself. It PROXIES the write to the owning app's control API
(``POST /internal/jobs/v1/{external_job_id}/{action}``), exchanging for that
app's audience. The producer maps the action onto its own job manager and
returns the new :class:`fm_runtime.JobStatus`.

Auth (flag for security review): the proxy uses a context-following
:class:`fm_runtime.InternalClient`, so the ACTING HUMAN'S token is exchanged
``jobs -> {app}`` (the human stays the subject; ``azp`` becomes ``jobs``). The
owning app authorizes the ``jobs`` caller on its ``/internal/jobs/v1/*`` control
API (its ``azp_allow`` lists ``jobs``); the exchange is gated by the
``svc-{app}`` optional client scope (v1: ``jobs->search``, ``jobs->agents``).
Control is a WRITE — distinct from the read-only stream subscription.

Idempotency: a job already in a terminal status is a no-op — its current status
is returned without calling the producer (a finished job has no live handle to
control, and the producer may already have dropped it after its post-job TTL).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from fm_runtime import InternalClient, JobControlAction, JobStatus
from fm_runtime.job_events import TERMINAL_STATUSES, control_path
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Job

logger = logging.getLogger(__name__)


def _parse_status(payload: object, fallback: JobStatus) -> JobStatus:
    """Read the new status from a producer's control response, leniently.

    Accepts ``{"status": "paused"}`` (the documented shape), a bare
    ``"paused"`` string, or anything else (→ ``fallback``, the status we sent
    the action for — the authoritative value still arrives on the stream)."""
    candidate: object = None
    if isinstance(payload, dict):
        candidate = payload.get("status")
    elif isinstance(payload, str):
        candidate = payload
    if candidate is None:
        return fallback
    try:
        return JobStatus.coerce(candidate)
    except ValueError:
        logger.warning("control response carried unknown status %r", candidate)
        return fallback


async def proxy_control(
    session: AsyncSession, job: Job, action: JobControlAction
) -> JobStatus:
    """Proxy ``action`` to ``job``'s owning app and return the new status.

    Optimistically updates the row's status from the producer's reply; the
    authoritative update still arrives via the stream subscriber."""
    current = JobStatus.coerce(job.status)
    if current in TERMINAL_STATUSES:
        # Idempotent no-op — cannot control a finished job.
        logger.info(
            "control %s on terminal job %s/%s is a no-op (%s)",
            action.value,
            job.app,
            job.external_job_id,
            current.value,
        )
        return current

    base_url = get_settings().producer_map.get(job.app)
    if not base_url:
        # The job's app is no longer a configured producer (config drift / a
        # removed producer with rows still present). We cannot reach it.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"no configured producer base URL for app {job.app!r}",
        )

    path = control_path(job.external_job_id, action)
    timeout = get_settings().control_timeout_seconds
    try:
        async with InternalClient(base_url, audience=job.app, timeout=timeout) as client:
            response = await client.post(path)
    except Exception as exc:  # httpx transport / exchange failure
        logger.warning("control %s -> %s%s failed: %s", action.value, base_url, path, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"control call to {job.app} failed: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"{job.app} rejected {action.value} on {job.external_job_id} "
                f"({response.status_code}): {response.text[:200]}"
            ),
        )

    try:
        payload: object = response.json()
    except ValueError:
        payload = response.text.strip()
    new_status = _parse_status(payload, current)

    # Optimistic local update; the stream event (ts-gated) remains authoritative.
    # Bump last_event_at alongside the status so an in-flight OLDER stream event
    # (buffered before this control action landed) cannot slip past store.py's
    # ts-ordering guard and transiently regress the status we just applied.
    job.status = new_status.value
    job.last_event_at = datetime.now(timezone.utc)
    await session.commit()
    return new_status
