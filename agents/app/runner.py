"""Runtime-AI-agent execution engine.

Runs a pydantic-ai agent that pursues a human's goal **exclusively through MCP
tools** (search/leads/jobs/mail), driving the loop node-by-node so each run is:

- a **job** — every lifecycle transition is published to the in-process
  ``job_registry`` and surfaces on ``/internal/jobs/v1/stream`` for the ``jobs``
  service (attributed "alice (via agent)");
- **controllable** — the ``jobs`` service can pause/resume/cancel a run; control
  is applied cooperatively between graph nodes (never a hard kill of a tool call
  mid-flight);
- **bounded** — request/tool-call usage limits + a wall-clock ceiling stop a
  planning loop from spinning unbounded (a resource guard in the spirit of the
  build plan's Principle 4).

The *runtime* agents' LLM is OpenAI, configurable via ``AGENTS_LLM_MODEL`` — it is
independent of whatever model builds this service.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fm_runtime import JobStatus, get_runtime_settings
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.jobs_registry import JobContext, publish_job
from app.models import (
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    AgentRun,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Funnel Manager's runtime operations agent. You complete a user's "
    "task by calling the MCP tools available to you — search, leads, jobs, and "
    "mail. You have NO other capabilities: you act ONLY through those tools, "
    "never by inventing data or claiming an action you did not take via a tool.\n\n"
    "Work method:\n"
    "1. Situational awareness FIRST. Before starting new work, use the jobs tools "
    "(list_jobs / get_job) to see what is already running for this user, and the "
    "read tools (list_searches, recent_leads, list_campaigns) to understand prior "
    "activity. Do not launch a duplicate of something already in flight — wait on "
    "or reference the existing job instead.\n"
    "2. Then take the minimal set of tool actions that achieve the goal.\n"
    "3. If a tool returns a confirmation_required response (an expensive action "
    "over a threshold), DO NOT auto-confirm. Stop and report the estimate to the "
    "user so a human can decide — escalation, never silent confirmation.\n"
    "4. When done, reply with a concise natural-language summary of exactly what "
    "you did (which tools, what results) and any follow-up the user should take."
)


def build_model(settings: Settings) -> OpenAIChatModel:
    """Construct the OpenAI chat model the runtime agent plans with.

    The key is read server-side from ``OPENAI_API_KEY`` (never hardcoded, never
    sent to a client); the model name is app config. Passing ``api_key=None`` when
    unset lets the provider fall back to its own env lookup and surface a clear
    error rather than us baking in a secret.
    """
    provider = OpenAIProvider(api_key=settings.openai_api_key or None)
    return OpenAIChatModel(settings.model_name, provider=provider)


class _Canceled(Exception):
    """Cooperative cancel raised between graph nodes."""


@dataclass
class RunHandle:
    """Live control surface for one in-flight run."""

    run_id: str
    ctx: JobContext
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set == "go"; cleared == "paused" (a checkpoint awaits it). Starts set.
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False
    task: asyncio.Task | None = None

    def request_pause(self) -> None:
        self.paused = True
        self.resume_event.clear()

    def request_resume(self) -> None:
        self.paused = False
        self.resume_event.set()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        # Unblock any checkpoint currently awaiting a resume.
        self.resume_event.set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _build_prompt(goal: str, params: dict[str, Any]) -> str:
    if not params:
        return goal
    lines = [f"- {k}: {v}" for k, v in params.items()]
    return goal + "\n\nStructured parameters supplied by the user:\n" + "\n".join(lines)


def _usage_dict(usage: Any) -> dict[str, Any]:
    try:
        return {
            "requests": getattr(usage, "requests", 0),
            "tool_calls": getattr(usage, "tool_calls", 0),
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
        }
    except Exception:  # usage snapshot is best-effort observability only
        return {}


class RunManager:
    """Owns the in-flight run tasks and applies job control onto them."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def start(
        self,
        *,
        run_id: str,
        goal: str,
        params: dict[str, Any],
        ctx: JobContext,
        subject_token: str | None,
    ) -> None:
        """Spawn the background run task. Detached from the request: the human's
        subject token is captured now and exchanged per MCP call until it
        expires, then the run downgrades to the service identity."""
        handle = RunHandle(run_id=run_id, ctx=ctx)
        handle.resume_event.set()
        self._runs[run_id] = handle
        handle.task = asyncio.create_task(
            self._execute(handle, goal, params, subject_token),
            name=f"agent-run-{run_id}",
        )

    async def control(self, run_id: str, action: str) -> tuple[str, bool]:
        """Apply pause/resume/cancel. Returns ``(new_status, applied)``.

        Idempotent: a run that already finished (no live handle) is looked up in
        the DB and its terminal status returned with ``applied=False``.
        """
        handle = self._runs.get(run_id)
        if handle is None:
            status = await self._db_status(run_id)
            return status or STATUS_COMPLETED, False
        if action == "pause":
            handle.request_pause()
            return STATUS_PAUSED, True
        if action == "resume":
            handle.request_resume()
            return STATUS_RUNNING, True
        if action == "cancel":
            handle.request_cancel()
            return STATUS_CANCELED, True
        return STATUS_RUNNING, False

    async def shutdown(self) -> None:
        for handle in list(self._runs.values()):
            if handle.task is not None:
                handle.task.cancel()
        for handle in list(self._runs.values()):
            if handle.task is not None:
                try:
                    await handle.task
                except (asyncio.CancelledError, Exception):
                    pass
        self._runs.clear()

    # --- execution --------------------------------------------------------

    async def _execute(
        self,
        handle: RunHandle,
        goal: str,
        params: dict[str, Any],
        subject_token: str | None,
    ) -> None:
        settings = get_settings()
        run_id = handle.run_id
        steps = 0
        try:
            await self._mark_running(run_id)
            await publish_job(
                job_id=run_id, ctx=handle.ctx, status=JobStatus.RUNNING, progress=0.0
            )

            # Local import to avoid a hard import cycle at module load and to keep
            # the MCP client construction close to where it is used.
            from app.mcp_client import build_mcp_toolset

            toolset = build_mcp_toolset(
                settings, subject_token=subject_token, origin=handle.ctx.origin
            )
            agent = Agent(
                build_model(settings),
                toolsets=[toolset],
                system_prompt=SYSTEM_PROMPT,
                name="funnel-runtime-agent",
            )
            limits = UsageLimits(
                request_limit=settings.run_request_limit,
                tool_calls_limit=settings.run_tool_calls_limit,
            )
            prompt = _build_prompt(goal, params)

            async with asyncio.timeout(settings.run_timeout_seconds):
                async with agent:  # opens the MCP toolset connection
                    async with agent.iter(prompt, usage_limits=limits) as agent_run:
                        async for _node in agent_run:
                            steps += 1
                            await self._checkpoint(handle, steps)
                    result = agent_run.result
                    output = result.output if result is not None else ""
                    # `usage` is a property on the AgentRun (a RunUsage snapshot),
                    # not a method — read it directly.
                    usage = _usage_dict(agent_run.usage)

            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.COMPLETED,
                result=str(output),
                usage=usage,
                steps=steps,
                exit_status="ok",
            )
        except _Canceled:
            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.CANCELED,
                steps=steps,
                error="canceled by control request",
                exit_status="canceled",
            )
        except (asyncio.TimeoutError, TimeoutError):
            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.FAILED,
                steps=steps,
                error=f"run exceeded time limit ({settings.run_timeout_seconds}s)",
                exit_status="timeout",
            )
        except UsageLimitExceeded as exc:
            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.FAILED,
                steps=steps,
                error=f"usage limit reached: {exc}",
                exit_status="usage_limit",
            )
        except asyncio.CancelledError:
            # Service shutdown — mark failed best-effort, then re-raise.
            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.FAILED,
                steps=steps,
                error="service shutting down",
                exit_status="shutdown",
            )
            raise
        except Exception as exc:
            logger.exception("agent run %s failed", run_id)
            await self._finish(
                run_id,
                handle.ctx,
                status=JobStatus.FAILED,
                steps=steps,
                error=str(exc),
                exit_status="error",
            )
        finally:
            self._runs.pop(run_id, None)

    async def _checkpoint(self, handle: RunHandle, steps: int) -> None:
        """Between graph nodes: honor cancel, apply pause, emit progress."""
        if handle.cancel_event.is_set():
            raise _Canceled()

        await self._bump_steps(handle.run_id, steps)
        await publish_job(
            job_id=handle.run_id,
            ctx=handle.ctx,
            status=JobStatus.RUNNING,
            meta={"steps": steps},
        )

        if handle.paused:
            await self._mark_status(handle.run_id, STATUS_PAUSED)
            await publish_job(
                job_id=handle.run_id,
                ctx=handle.ctx,
                status=JobStatus.PAUSED,
                meta={"steps": steps},
            )
            await handle.resume_event.wait()
            if handle.cancel_event.is_set():
                raise _Canceled()
            await self._mark_status(handle.run_id, STATUS_RUNNING)
            await publish_job(
                job_id=handle.run_id,
                ctx=handle.ctx,
                status=JobStatus.RUNNING,
                meta={"steps": steps},
            )

    # --- persistence ------------------------------------------------------

    async def _mark_running(self, run_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.status = STATUS_RUNNING
                run.started_at = _utcnow()
                await session.commit()

    async def _mark_status(self, run_id: str, status: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.status = status
                await session.commit()

    async def _bump_steps(self, run_id: str, steps: int) -> None:
        async with SessionLocal() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.steps = steps
                await session.commit()

    async def _db_status(self, run_id: str) -> str | None:
        async with SessionLocal() as session:
            run = await session.get(AgentRun, run_id)
            return run.status if run is not None else None

    async def _finish(
        self,
        run_id: str,
        ctx: JobContext,
        *,
        status: JobStatus,
        steps: int,
        result: str | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        exit_status: str | None = None,
    ) -> None:
        async with SessionLocal() as session:
            run = await session.get(AgentRun, run_id)
            if run is not None:
                run.status = status.value
                run.steps = steps
                run.ended_at = _utcnow()
                if result is not None:
                    run.result = result
                if error is not None:
                    run.error = error
                if usage is not None:
                    run.usage = usage
                if status is JobStatus.COMPLETED:
                    run.progress = 1.0
                await session.commit()
        await publish_job(
            job_id=run_id,
            ctx=ctx,
            status=status,
            progress=1.0 if status is JobStatus.COMPLETED else None,
            exit_status=exit_status,
            meta={"steps": steps},
        )


def agents_actor() -> str:
    """The agents service's own OAuth client id (``azp`` it acts under) — recorded
    as a run's ``actor`` so it renders "alice (via agent)"."""
    return get_runtime_settings().effective_client_id or "agents"


run_manager = RunManager()

__all__ = ["RunManager", "agents_actor", "build_model", "run_manager"]
