"""Search's job producer for the ``/internal/jobs/v1`` stream.

Search is a **job producer**: every Apollo ingest, its embedding pass, and each
semantic search publishes lifecycle ``JobEvent``s here. The internal stream
endpoint (``GET /internal/jobs/v1/stream``) fans them out to the ``jobs`` service,
which persists job state and offers cross-user visibility + control.

The broadcast hub, the active-only subscribe snapshot, the TTL prune, the
never-raise NDJSON generator, and the control apply-then-republish scaffold are
**not** hand-rolled here — they live once in :class:`fm_runtime.JobProducer`
(P10: cross-cutting producer plumbing belongs in ``fm_runtime``, not
re-implemented per service). This module is the **reference wiring** onto that
chassis: it constructs the single :data:`job_producer` and supplies search's one
engine callback (``apply_control`` — mapping a control action onto the leads
stream engine). No ``enumerate_active_jobs`` callback is supplied: search keeps
no durable out-of-band job store, so the producer's own in-memory latest-event
cache already reconstructs the active snapshot.

Design notes:
- ``job_id`` is the underlying **leads stream id** (ingest or embedding). That
  makes control trivial and unambiguous: pausing/cancelling a job maps 1:1 onto
  the leads stream control hook, and the id the jobs service stores is the exact
  handle search uses upstream. Semantic searches have no leads stream, so they
  get a synthetic ``sem-<search_id>`` id and only ever emit a terminal event.
- ``JobEvent`` is constructed **strictly per fm_runtime** (real ``JobStatus``
  enums, no bare status strings) so a typo fails loud in-process rather than
  silently degrading a job's reported lifecycle.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from fm_runtime import JobControlAction, JobEvent, JobProducer, JobStatus

from app.config import Settings, get_settings
from app.leads_client import LeadsClient

logger = logging.getLogger(__name__)

# Job type vocabulary (search owns its own; free-form on the wire).
JOB_TYPE_APOLLO_SEARCH = "apollo_search"
JOB_TYPE_EMBEDDING = "embedding"
JOB_TYPE_SEMANTIC_SEARCH = "semantic_search"

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


@dataclass
class JobContext:
    """Attribution + linkage carried alongside a search's job events."""

    user: str
    origin: str
    actor: str
    search_id: int
    job_type: str = JOB_TYPE_APOLLO_SEARCH


# -- per-request control plumbing -----------------------------------------
#
# ``JobProducer.dispatch_control`` calls ``apply_control(job_id, action)`` with a
# fixed signature, so the acting human (audit-only) and the leads-control outcome
# (status + whether it applied — needed for the HTTP response) are threaded
# through request-scoped contextvars rather than extra args. The control route
# opens :func:`control_request` around its ``dispatch_control`` call.
_acting_user_var: ContextVar[str] = ContextVar("_jobs_acting_user", default="")
_control_outcome_var: ContextVar["tuple[JobStatus, bool] | None"] = ContextVar(
    "_jobs_control_outcome", default=None
)


@contextmanager
def control_request(acting_user: str) -> Iterator[None]:
    """Scope one control request: expose ``acting_user`` to :func:`_apply_control`
    (recorded as audit metadata on the re-published event) and capture the
    leads-control outcome so the route can build its response. Read
    :func:`last_control_outcome` *inside* the ``with`` block."""
    au_token = _acting_user_var.set(acting_user)
    out_token = _control_outcome_var.set(None)
    try:
        yield
    finally:
        _control_outcome_var.reset(out_token)
        _acting_user_var.reset(au_token)


def last_control_outcome() -> "tuple[JobStatus, bool] | None":
    """The ``(new_status, applied)`` the most recent ``apply_control`` computed in
    this request scope, or ``None`` if control did not reach the engine."""
    return _control_outcome_var.get()


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


async def _apply_control(job_id: str, action: JobControlAction) -> JobEvent | None:
    """Search's ``apply_control`` engine callback for :class:`fm_runtime.JobProducer`.

    Applies the control action to the leads stream engine (under search's own
    identity) and returns the resulting ``JobEvent`` for the producer to
    re-publish — or ``None`` when there is nothing to re-publish (an unknown job
    the in-memory hub never saw; control is still proxied to leads and the outcome
    still reaches the caller via :func:`last_control_outcome`). The ``(status,
    applied)`` outcome is always stashed for the HTTP response.
    """
    # A semantic search job (`sem-<id>`) has no leads stream — nothing to control.
    if job_id.startswith("sem-"):
        raise HTTPException(
            status_code=409,
            detail="semantic search jobs are synchronous and cannot be controlled",
        )

    result = await _leads_control(get_settings(), job_id, action.value)
    leads_status = str(result.get("status") or "").strip().lower()
    new_status = _LEADS_STATUS_TO_JOB.get(leads_status, JobStatus.RUNNING)
    applied = bool(result.get("applied"))
    _control_outcome_var.set((new_status, applied))

    # Re-publish a JobEvent so the jobs store reflects the control outcome. Reuse
    # the known job's attribution/type when we have it; record the acting human.
    # An unknown job (search restarted, hub lost the row) proxies control to leads
    # but publishes nothing — there is no attribution to reconstruct.
    prior = await job_producer.latest(job_id)
    if prior is None:
        return None
    acting_user = _acting_user_var.get()
    return JobEvent(
        job_id=job_id,
        type=prior.type,
        user=prior.user,
        origin=prior.origin,
        actor=prior.actor,
        status=new_status,
        progress=prior.progress,
        exit_status="ok" if new_status is JobStatus.CANCELED else None,
        meta={**prior.meta, "control": action.value, "acting_user": acting_user},
    )


#: The single producer instance for search. No ``enumerate_active_jobs`` callback:
#: search has no durable job store, so the producer's own in-memory latest-event
#: cache is the authoritative active snapshot.
job_producer = JobProducer(apply_control=_apply_control, log=logger)


async def publish_job(
    *,
    job_id: str,
    job_type: str,
    ctx: JobContext | None,
    status: JobStatus,
    progress: float | None = None,
    exit_status: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Construct a JobEvent (strict) and broadcast it. No-op without a ctx."""
    if ctx is None or not str(job_id or "").strip():
        return
    event = JobEvent(
        job_id=str(job_id),
        type=job_type,
        user=ctx.user,
        origin=ctx.origin,
        actor=ctx.actor,
        status=status,
        progress=progress,
        exit_status=exit_status,
        meta={"search_id": ctx.search_id, **(meta or {})},
    )
    await job_producer.publish(event)


def job_progress_fraction(done: int, total: int) -> float | None:
    """Clamp done/total to 0.0-1.0, or None when total is unknown."""
    if total <= 0:
        return None
    return max(0.0, min(1.0, done / total))


__all__ = [
    "JOB_TYPE_APOLLO_SEARCH",
    "JOB_TYPE_EMBEDDING",
    "JOB_TYPE_SEMANTIC_SEARCH",
    "JobContext",
    "control_request",
    "job_producer",
    "job_progress_fraction",
    "last_control_outcome",
    "publish_job",
]
