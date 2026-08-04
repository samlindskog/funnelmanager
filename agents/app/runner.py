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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fm_runtime import (
    JobStatus,
    approval_ref as make_approval_ref,
    get_runtime_settings,
)
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.approvals import (
    approval_coordinator,
    create_pending_approval,
    expire_pending_for_run,
    is_ref_consumed,
    mark_ref_consumed,
)
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
    "3. Expensive actions are HUMAN-gated. You must NEVER set confirm=true and "
    "NEVER pass a human_approval token yourself — you cannot self-approve an "
    "expensive action. If a tool reports it needs human approval, this service "
    "pauses the run and asks your human out of band; when they approve, the SAME "
    "action is automatically re-issued for you with their approval, so you do not "
    "need to retry it. If a tool result says a human REJECTED the action, do not "
    "retry it — respect the decision and continue with the rest of the goal or "
    "stop.\n"
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
    # The live run-timeout context manager, so a human-approval wait can pause
    # the wall-clock deadline (a person may take minutes to approve; that time
    # must not count against the run's budget). Set once agent.iter is entered.
    timeout_cm: Any = None
    _timeout_remaining: float | None = None
    _timeout_suspends: int = 0
    _timeout_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # ids of pending approvals this run created — expired + discarded on finish.
    approval_ids: set[str] = field(default_factory=set)

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

    async def suspend_timeout(self) -> None:
        """Freeze the run's wall-clock deadline while awaiting human approval.

        Reference-counted so concurrent gated tool calls compose: the deadline is
        disabled on the first suspend and its remaining budget preserved, then
        restored only when the last approval wait finishes."""
        cm = self.timeout_cm
        if cm is None:
            return
        async with self._timeout_lock:
            self._timeout_suspends += 1
            if self._timeout_suspends == 1:
                when = cm.when()
                if when is not None:
                    self._timeout_remaining = max(0.0, when - asyncio.get_running_loop().time())
                    cm.reschedule(None)  # disable the deadline

    async def resume_timeout(self) -> None:
        cm = self.timeout_cm
        if cm is None:
            return
        async with self._timeout_lock:
            if self._timeout_suspends == 0:
                return
            self._timeout_suspends -= 1
            if self._timeout_suspends == 0 and self._timeout_remaining is not None:
                cm.reschedule(asyncio.get_running_loop().time() + self._timeout_remaining)
                self._timeout_remaining = None


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


# Gate marker fm_runtime.confirmation raises for an over-threshold AGENT action
# (AgentApprovalRequired). The MCP BackendClient surfaces it verbatim as a tool
# result (not an error), so a tool call returns this shape instead of running.
_HUMAN_APPROVAL_ERROR = "human_approval_required"


def _extract_approval_gate(result: Any) -> dict[str, Any] | None:
    """The fm_runtime agent-approval gate detail if a tool result is one, else None.

    A runtime agent can never self-confirm, so an over-threshold agent action
    comes back as a structured ``needs_human_approval`` payload rather than an
    error or a completed action. We detect ONLY that shape — an ordinary
    ``confirmation_required`` (the human confirm=true flow) is never actioned here
    (an agent must not answer it), and every other result flows straight through.
    """
    payload = result
    if isinstance(payload, dict):
        # BackendClient already unwraps `detail`, but be defensive if a raw
        # HTTPException body ({"detail": {...}}) ever reaches us.
        detail = payload.get("detail", payload)
        if isinstance(detail, dict) and (
            detail.get("needs_human_approval") is True
            or detail.get("error") == _HUMAN_APPROVAL_ERROR
        ):
            return detail
    return None


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

            # process_tool_call is the single interception point for the
            # Principle-4 agent gate: it detects a needs_human_approval tool
            # result, pauses the run for its human, and — once approved — re-issues
            # the SAME call with the minted token (the LLM never touches the gate).
            toolset = build_mcp_toolset(
                settings,
                subject_token=subject_token,
                origin=handle.ctx.origin,
                process_tool_call=self._make_process_tool_call(handle),
            )
            agent = Agent(
                build_model(settings),
                toolsets=[toolset],
                system_prompt=SYSTEM_PROMPT,
                name="funnel-runtime-agent",
                # Emit pydantic-ai spans (agent run / model request / tool call +
                # token usage) explicitly per-agent. main.py calls
                # logfire.instrument_pydantic_ai() to set the global default, but in
                # the pinned pydantic-ai that global was NOT picked up by agents left
                # at instrument=None (verified live: only FastAPI spans reached
                # Logfire, no LLM/tool spans). The explicit flag is version-safe and
                # a no-op when no OTel tracer provider is configured (prod: FM_LOGFIRE
                # unset -> logfire never configured -> spans go nowhere).
                instrument=True,
            )
            limits = UsageLimits(
                request_limit=settings.run_request_limit,
                tool_calls_limit=settings.run_tool_calls_limit,
            )
            prompt = _build_prompt(goal, params)

            async with asyncio.timeout(settings.run_timeout_seconds) as timeout_cm:
                # Expose the deadline so a human-approval wait can freeze it.
                handle.timeout_cm = timeout_cm
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
            # Any approval left undecided can never be acted on now the run is
            # terminal — expire the rows and drop the in-memory waiters so a late
            # approve/reject cannot resolve a dead run.
            try:
                await expire_pending_for_run(run_id)
            except Exception:  # cleanup is best-effort; never mask the run outcome
                logger.exception("agent run %s: failed to expire pending approvals", run_id)
            for approval_id in list(handle.approval_ids):
                approval_coordinator.discard(approval_id)
            handle.approval_ids.clear()
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

    # --- Principle-4 human-approval gate ----------------------------------

    def _make_process_tool_call(self, handle: RunHandle):
        """Build the pydantic-ai ``process_tool_call`` hook for this run.

        It wraps every MCP tool call: run it, and if the result is the fm_runtime
        agent-approval gate (``needs_human_approval``), hand off to the approval
        flow — pause the run, record a pending approval, wait for the initiating
        human's decision, then either re-issue the exact call WITH the minted
        token (approved) or surface the rejection to the model (rejected)."""

        async def process_tool_call(ctx, call_tool, name: str, tool_args: dict[str, Any]):
            result = await call_tool(name, tool_args)
            gate = _extract_approval_gate(result)
            if gate is None:
                return result
            return await self._await_approval(handle, call_tool, name, tool_args, gate)

        return process_tool_call

    async def _await_approval(
        self,
        handle: RunHandle,
        call_tool,
        name: str,
        tool_args: dict[str, Any],
        gate: dict[str, Any],
    ) -> Any:
        """Pause the run on an over-threshold agent action until the initiating
        human decides, then re-issue (approved) or report the rejection."""
        run_id = handle.run_id
        subject = handle.ctx.user  # the initiating human = the run owner
        estimate = float(gate.get("estimate") or 0.0)
        action = str(gate.get("action") or "")
        threshold = gate.get("threshold")
        unit = str(gate.get("unit") or "")
        message = str(gate.get("message") or "")
        # Trust our own recomputed ref (never a value the upstream made up) so it
        # is bound to exactly this subject — matches what the token binds to.
        ref = make_approval_ref(action, estimate, subject)

        approval_id = uuid.uuid4().hex
        # Register the waiter BEFORE persisting the row so a human can never
        # resolve an approval the run is not yet awaiting.
        approval_coordinator.register(approval_id)
        handle.approval_ids.add(approval_id)
        await create_pending_approval(
            approval_id=approval_id,
            run_id=run_id,
            subject=subject,
            approval_ref=ref,
            action=action,
            estimate=estimate,
            threshold=float(threshold) if isinstance(threshold, (int, float)) else None,
            unit=unit,
            message=message,
            tool_name=name,
            tool_args=tool_args,
        )

        logger.info(
            "agent run %s paused for human approval: action=%s estimate=%s%s ref=%s",
            run_id, action, estimate, (" " + unit) if unit else "", ref,
        )

        # Cooperatively pause: freeze the wall-clock deadline (a human may take a
        # while) and surface a PAUSED job event flagged as awaiting approval.
        await handle.suspend_timeout()
        await self._mark_status(run_id, STATUS_PAUSED)
        approval_meta = {
            "awaiting_approval": True,
            "approval_id": approval_id,
            "approval_ref": ref,
            "action": action,
            "estimate": estimate,
        }
        await publish_job(
            job_id=run_id, ctx=handle.ctx, status=JobStatus.PAUSED, meta=approval_meta
        )

        try:
            decision = await approval_coordinator.wait(approval_id, handle.cancel_event)
        finally:
            await handle.resume_timeout()
            approval_coordinator.discard(approval_id)

        if decision is None:
            # Canceled while waiting: abandon this call. The next checkpoint sees
            # cancel_event and unwinds the run cleanly (no raise from inside a
            # tool call, which pydantic-ai might wrap/retry).
            return {
                "error": "canceled",
                "message": "The run was canceled while awaiting human approval; "
                "this action was not performed.",
                "action": action,
            }

        # Decided — resume the run's reported status.
        await self._mark_status(run_id, STATUS_RUNNING)
        await publish_job(
            job_id=run_id,
            ctx=handle.ctx,
            status=JobStatus.RUNNING,
            meta={"approval_id": approval_id, "approved": decision.approved},
        )

        if not decision.approved:
            return {
                "error": "human_rejected",
                "message": "A human reviewed this expensive action and REJECTED it. "
                "Do not retry it; continue with the rest of the task or stop.",
                "action": action,
            }

        # Single-use guard (belt-and-suspenders): if this ref was spent between
        # the human approving and now — a concurrent action for the same bucket —
        # do NOT re-issue. The still-valid token would otherwise replay for a
        # second over-threshold action. Surface it to the model, don't retry.
        if await is_ref_consumed(ref):
            return {
                "error": "approval_spent",
                "message": "This action's human approval was already used for "
                "another action and cannot be reused; do not retry it.",
                "action": action,
            }

        # Approved: re-issue the EXACT call with the human-minted token injected
        # server-side (never by the LLM). The gate now allows it.
        approved_args = {**tool_args, "human_approval": decision.token}
        reissued = await call_tool(name, approved_args)
        # Mark the approval_ref consumed once the gate actually let the action
        # through (result is no longer a needs_human_approval gate) — single-use:
        # this ref can now never authorize a second over-threshold action, and a
        # later approval for the same bucket is rejected up front. A still-gated
        # result (e.g. token/subject mismatch) means the action did NOT run, so we
        # deliberately do not consume — and we return it as-is rather than loop
        # back into the approval flow, which could spin.
        if _extract_approval_gate(reissued) is None:
            await mark_ref_consumed(
                approval_ref=ref,
                run_id=run_id,
                approval_id=approval_id,
                subject=subject,
                action=action,
                estimate=estimate,
            )
        return reissued

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
