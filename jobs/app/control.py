"""Job-control proxy.

The `jobs` service does not run work, so it cannot pause/resume/cancel a job
itself. It PROXIES the write to the owning app's control API
(``POST /internal/jobs/v1/{external_job_id}/{action}``), exchanging for that
app's audience. The producer maps the action onto its own job manager and
returns the new :class:`fm_runtime.JobStatus`.

Trust boundary (Phase 2, flag for security review). A producer's
``/internal/jobs/v1/*`` endpoints — BOTH the stream read and this control write —
are callable ONLY by the `jobs` service, authorized by a dedicated
``jobs-internal`` realm role held by the ``jobs`` client's SERVICE ACCOUNT. So
the proxy authenticates as the JOBS SERVICE ACCOUNT (client-credentials,
``azp = jobs``), NOT the acting human — a non-context-following
:class:`fm_runtime.InternalClient` (``follow_context=False``, no subject token)
mints the same client-credentials ``jobs -> {app}`` token the read-only stream
subscriber uses (:mod:`app.subscriber`), gated by the same ``svc-{app}`` optional
client scope (v1: ``jobs->search``, ``jobs->agents``) and the same
``jobs-internal`` grant.

The acting human never crosses into the producer as a token; it rides along as
AUDIT METADATA only (``X-FM-Acting-User`` + origin/actor headers, derived from
the request principal). Authorization of the human happens EARLIER, at the jobs
MCP API (audience ``jobs`` + ``jobs-access`` grant via PrincipalMiddleware /
OPA), before this proxy is ever reached — the producer trusts the ``jobs``
service and treats the header purely as attribution ("alice", "alice (via
agent)").

Idempotency: a job already in a terminal status is a no-op — its current status
is returned without calling the producer (a finished job has no live handle to
control, and the producer may already have dropped it after its post-job TTL).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from fm_runtime import InternalClient, JobControlAction, JobStatus, Principal
from fm_runtime.job_events import TERMINAL_STATUSES, control_path
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Job

logger = logging.getLogger(__name__)

# Audit-metadata headers carried on the control call. The producer authenticates
# the CALLER as the `jobs` service account; these identify the human who
# authorized the action at the jobs MCP API so the producer can attribute it.
ACTING_USER_HEADER = "X-FM-Acting-User"  # preferred_username of the human
ACTING_ORIGIN_HEADER = "X-FM-Acting-Origin"  # user | agent (fm_origin)
ACTING_ACTOR_HEADER = "X-FM-Acting-Actor"  # azp of the human's inbound token


def _audit_headers(principal: Principal) -> dict[str, str]:
    """Build the acting-human audit headers from the request principal.

    Only non-empty values are sent. This is attribution, NOT authorization — the
    human was already authorized at the jobs MCP API, and the control call itself
    authenticates as the jobs service account."""
    headers: dict[str, str] = {}
    if principal.username:
        headers[ACTING_USER_HEADER] = principal.username
    if principal.origin:
        headers[ACTING_ORIGIN_HEADER] = principal.origin
    if principal.actor:
        headers[ACTING_ACTOR_HEADER] = principal.actor
    return headers


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
    session: AsyncSession, job: Job, action: JobControlAction, principal: Principal
) -> JobStatus:
    """Proxy ``action`` to ``job``'s owning app and return the new status.

    The call authenticates as the JOBS SERVICE ACCOUNT (client-credentials,
    ``azp = jobs``); ``principal`` is the already-authorized acting human, sent
    only as audit metadata (:func:`_audit_headers`). Optimistically updates the
    row's status from the producer's reply; the authoritative update still
    arrives via the stream subscriber."""
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
        # follow_context=False + no subject_token => the broker mints the jobs
        # service account's client-credentials token (azp=jobs, jobs-internal
        # role), exactly as the read-only stream subscriber does. The human is
        # NOT the subject; it travels as audit headers only.
        async with InternalClient(
            base_url,
            audience=job.app,
            follow_context=False,
            subject_token=None,
            timeout=timeout,
        ) as client:
            response = await client.post(path, headers=_audit_headers(principal))
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
