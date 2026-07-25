"""Jobs tools — over the jobs backend's MCP surface (``/api/jobs/mcp/v1/*``).

Exchange edge: ``mcp->jobs``. These give a runtime agent situational awareness
(what is running) and control (pause/resume/cancel). Jobs are cross-user visible
(the ``user`` filter is an attribute, not an access gate).

``list_jobs``/``get_job`` are ``readOnlyHint``. Control tools are idempotent
(re-issuing an action returns the resulting status); ``cancel_job`` additionally
carries ``destructiveHint`` (it terminates the job).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.deps import Deps
from app.tools._shared import effective_token

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_CONTROL = ToolAnnotations(
    readOnlyHint=False, idempotentHint=True, destructiveHint=False
)
_CANCEL = ToolAnnotations(
    readOnlyHint=False, idempotentHint=True, destructiveHint=True
)


def register(mcp: FastMCP, deps: Deps) -> None:
    jobs = deps.jobs

    @mcp.tool(annotations=_READ_ONLY)
    async def list_jobs(
        user: str | None = None,
        app: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """List tracked jobs, most recently active first (cross-user by design).
        Optional filters: user (human owner), app (producing service, e.g. search
        or agents), status (queued|running|paused|completed|failed|canceled)."""
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 500)),
            "offset": max(0, int(offset)),
        }
        if user is not None:
            params["user"] = user
        if app is not None:
            params["app"] = app
        if status is not None:
            params["status"] = status
        return await jobs.request(
            "GET",
            "/api/jobs/mcp/v1/jobs",
            params=params,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_job(
        job_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Get one job with its current status and progress. Cross-user visible."""
        return await jobs.request(
            "GET",
            f"/api/jobs/mcp/v1/jobs/{int(job_id)}",
            token=effective_token(session_token, ctx),
        )

    async def _control(
        job_id: int, action: Literal["pause", "resume", "cancel"], token: str | None
    ) -> dict[str, Any]:
        return await jobs.request(
            "POST", f"/api/jobs/mcp/v1/jobs/{int(job_id)}/{action}", token=token
        )

    @mcp.tool(annotations=_CONTROL)
    async def pause_job(
        job_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Pause a running job (proxied to its owning app). Idempotent — returns
        the resulting status; pausing a terminal job is a no-op."""
        return await _control(job_id, "pause", effective_token(session_token, ctx))

    @mcp.tool(annotations=_CONTROL)
    async def resume_job(
        job_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Resume a paused job (proxied to its owning app). Idempotent — returns
        the resulting status."""
        return await _control(job_id, "resume", effective_token(session_token, ctx))

    @mcp.tool(annotations=_CANCEL)
    async def cancel_job(
        job_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Cancel a job (proxied to its owning app) — terminal and non-recoverable.
        Idempotent: cancelling an already-terminal job returns its status."""
        return await _control(job_id, "cancel", effective_token(session_token, ctx))
