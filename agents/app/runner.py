"""Runtime-AI-agent **turn** execution engine.

A *turn* = one user message → the agent's streamed response (possibly many model
requests + tool calls). Each turn:

- runs as a **detached** ``asyncio`` task (survives the tab closing) driving the
  pydantic-ai ``agent.iter()`` node loop, so pause/cancel/checkpoint can be applied
  cooperatively **between** graph nodes (never a hard kill mid tool call);
- **streams** typed events (``message_start``/``text_delta``/``thinking_delta``/
  ``tool_call``/``tool_result``/``approval_required``/``summary``/``usage``/
  ``turn_complete``/``error``) into the per-session fan-out+replay buffer
  (``session_turn_manager``) so live subscribers AND reattaching ones see them;
- is a short **job** (``agent_turn``) published to the ``jobs`` service via
  ``jobs_registry`` (RUNNING → PAUSED on HITL → terminal), attributed
  "alice (via agent)";
- persists its pydantic-ai ``ModelMessage``s **verbatim** and stamps the per-turn
  ``RunUsage`` on the final assistant row (``app.history``);
- is **bounded** — request/tool-call usage limits + a wall-clock ceiling.

The *runtime* agents' LLM is OpenAI (``AGENTS_LLM_MODEL`` default; per-session
override), independent of whatever model builds this service.
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
# opentelemetry-api ships with pydantic-ai; this is only the OTel *context* API
# (safe even in a prod pod that never configures tracing), used to reset a
# detached turn to a fresh root trace — see _execute.
from opentelemetry import context as otel_context
from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.approvals import (
    approval_coordinator,
    create_pending_approval,
    expire_pending_for_turn,
    is_ref_consumed,
    mark_ref_consumed,
)
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.history import (
    _SummaryState,
    load_context_messages,
    make_history_processor,
    next_seq,
    persist_failed_user_message,
    persist_turn,
)
from app.jobs_registry import JobContext, publish_job
from app.models import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    AgentSession,
)
from app.models_catalog import context_window_for, estimate_cost
from app.session_manager import TurnStatus, session_turn_manager

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Funnel Manager's runtime operations agent, chatting with a user. You "
    "complete tasks by calling the MCP tools available to you — search, leads, "
    "jobs, and mail. You act ONLY through those tools, never by inventing data or "
    "claiming an action you did not take via a tool.\n\n"
    "Work method:\n"
    "1. Situational awareness FIRST. Before starting new work, use the jobs tools "
    "(list_jobs / get_job) to see what is already running for this user, and the "
    "read tools (list_searches, recent_leads, list_campaigns) to understand prior "
    "activity. Do not launch a duplicate of something already in flight — wait on "
    "or reference the existing job instead.\n"
    "2. Then take the minimal set of tool actions that achieve the goal. If the "
    "user asks for work to happen LATER or on a RECURRING schedule (e.g. 'every "
    "Monday', 'in an hour', 'tomorrow at 9'), use the schedule_agent_job tool to "
    "schedule it instead of trying to do it now — it runs in the background and "
    "survives the tab closing, and surfaces as a job the user can cancel.\n"
    "3. Expensive actions are HUMAN-gated. You must NEVER set confirm=true and "
    "NEVER pass a human_approval token yourself — you cannot self-approve an "
    "expensive action. If a tool reports it needs human approval, this service "
    "pauses the turn and asks your human; when they approve, the SAME action is "
    "automatically re-issued for you with their approval, so you do not need to "
    "retry it. If a tool result says a human REJECTED the action, do not retry it "
    "— respect the decision and continue with the rest of the task or stop.\n"
    "4. Reply conversationally with a concise summary of what you did (which "
    "tools, what results) and any follow-up the user should take."
)


def build_model(settings: Settings, model_name: str | None = None) -> OpenAIChatModel:
    """Construct the OpenAI chat model for a turn.

    ``model_name`` (the session's selected model, bare — no provider prefix) wins;
    otherwise the app-config default. Pinning ``OpenAIChatModel`` keeps us on the
    Chat Completions API (avoids the ``'openai:'``→Responses-API default). The key
    is read server-side from ``OPENAI_API_KEY`` — never hardcoded, never sent to a
    client.
    """
    provider = OpenAIProvider(api_key=settings.openai_api_key or None)
    name = (model_name or "").strip() or settings.model_name
    return OpenAIChatModel(name, provider=provider)


class _Canceled(Exception):
    """Cooperative cancel raised between graph nodes."""


@dataclass
class TurnHandle:
    """Live control surface for one in-flight turn."""

    turn_id: str
    session_id: str
    ctx: JobContext
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = False
    task: asyncio.Task | None = None
    timeout_cm: Any = None
    _timeout_remaining: float | None = None
    _timeout_suspends: int = 0
    _timeout_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    approval_ids: set[str] = field(default_factory=set)
    # Turn context captured so the failure path can best-effort persist the user's
    # own message when a turn dies before persist_turn runs (append-only invariant
    # — otherwise the input, and the next turn's memory of it, are lost).
    user_message: str = ""
    model_name: str = ""
    start_seq: int | None = None

    def request_pause(self) -> None:
        self.paused = True
        self.resume_event.clear()

    def request_resume(self) -> None:
        self.paused = False
        self.resume_event.set()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        self.resume_event.set()

    async def suspend_timeout(self) -> None:
        """Freeze the turn's wall-clock deadline while awaiting human approval.
        Reference-counted so concurrent gated calls compose."""
        cm = self.timeout_cm
        if cm is None:
            return
        async with self._timeout_lock:
            self._timeout_suspends += 1
            if self._timeout_suspends == 1:
                when = cm.when()
                if when is not None:
                    self._timeout_remaining = max(0.0, when - asyncio.get_running_loop().time())
                    cm.reschedule(None)

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


def _usage_dict(usage: Any) -> dict[str, Any]:
    try:
        return {
            "requests": int(getattr(usage, "requests", 0) or 0),
            "tool_calls": int(getattr(usage, "tool_calls", 0) or 0),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
        }
    except Exception:  # noqa: BLE001 — usage is best-effort observability
        return {}


def _jsonable(value: Any) -> Any:
    """Coerce a tool-result payload to something JSON-serializable for streaming."""
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


# Gate marker fm_runtime.confirmation returns for an over-threshold AGENT action.
_HUMAN_APPROVAL_ERROR = "human_approval_required"


def _extract_approval_gate(result: Any) -> dict[str, Any] | None:
    """The fm_runtime agent-approval gate detail if a tool result is one, else None.

    A runtime agent can never self-confirm, so an over-threshold agent action comes
    back as a structured ``needs_human_approval`` payload (a tool RESULT, never a
    raised error — see mcp_client fix #31). We detect ONLY that shape; an ordinary
    human ``confirmation_required`` is never actioned here.
    """
    payload = result
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict) and (
            detail.get("needs_human_approval") is True
            or detail.get("error") == _HUMAN_APPROVAL_ERROR
        ):
            return detail
    return None


class TurnRunner:
    """Owns in-flight turn tasks and applies job control onto them."""

    def __init__(self) -> None:
        self._turns: dict[str, TurnHandle] = {}

    def start_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_message: str,
        model: str,
        ctx: JobContext,
        subject_token: str | None,
    ) -> TurnHandle:
        """Spawn the detached turn task. The human's subject token is captured now
        and exchanged per MCP call until it genuinely expires (then leads-only
        service identity — see mcp_client)."""
        handle = TurnHandle(turn_id=turn_id, session_id=session_id, ctx=ctx)
        handle.resume_event.set()
        self._turns[turn_id] = handle
        handle.task = asyncio.create_task(
            self._execute(handle, user_message, model, subject_token),
            name=f"agent-turn-{turn_id}",
        )
        return handle

    async def control(self, turn_id: str, action: str) -> tuple[str, bool]:
        """Apply pause/resume/cancel to a turn (job_id == turn_id). Idempotent: a
        turn with no live handle returns ``(completed, False)``."""
        handle = self._turns.get(turn_id)
        if handle is None:
            return "completed", False
        if action == "pause":
            handle.request_pause()
            return STATUS_PAUSED, True
        if action == "resume":
            handle.request_resume()
            return STATUS_RUNNING, True
        if action == "cancel":
            handle.request_cancel()
            return "canceled", True
        return STATUS_RUNNING, False

    async def shutdown(self) -> None:
        for handle in list(self._turns.values()):
            if handle.task is not None:
                handle.task.cancel()
        for handle in list(self._turns.values()):
            if handle.task is not None:
                try:
                    await handle.task
                except (asyncio.CancelledError, Exception):
                    pass
        self._turns.clear()

    # --- execution --------------------------------------------------------

    async def _execute(
        self,
        handle: TurnHandle,
        user_message: str,
        model: str,
        subject_token: str | None,
    ) -> None:
        settings = get_settings()
        turn_id = handle.turn_id
        session_id = handle.session_id

        # A detached turn is its OWN trace. Reset the OTel context to an empty
        # (parent-less) context so this turn's pydantic-ai spans (agent / model
        # request / tool call + token usage) become a NEW ROOT trace rather than
        # inheriting the spawning request's context. That request rides the mesh's
        # 5% sampling (almost always sampled=0); OTel's parent-based sampler would
        # otherwise make every agent span non-recording and drop it. LOAD-BEARING —
        # do not remove (verified in-pod: fresh ctx -> spans emit).
        otel_reset = otel_context.attach(otel_context.Context())
        summary_state = _SummaryState()
        model_name = (model or "").strip() or settings.model_name
        handle.user_message = user_message
        handle.model_name = model_name

        try:
            await publish_job(
                job_id=turn_id, ctx=handle.ctx, status=JobStatus.RUNNING, progress=0.0,
                meta={"session_id": session_id},
            )
            await session_turn_manager.publish(
                turn_id, {"type": "message_start", "role": "user", "text": user_message}
            )

            from app.mcp_client import build_mcp_toolset

            toolset = build_mcp_toolset(
                settings,
                subject_token=subject_token,
                origin=handle.ctx.origin,
                process_tool_call=self._make_process_tool_call(handle),
            )

            # Local (non-MCP) scheduling tool: lets this turn schedule future/
            # recurring work in the agents DB (no new mcp->agents edge). The
            # expensive downstream calls the scheduled turn later makes stay
            # P4-gated at the MCP layer — only the cheap schedule write is local.
            from app.scheduler import build_schedule_tool

            schedule_tool = build_schedule_tool(
                session_id=session_id, ctx=handle.ctx, subject_token=subject_token
            )

            context_window = context_window_for(model_name)
            start_seq = await next_seq(session_id)
            handle.start_seq = start_seq
            watermark = await self._session_watermark(session_id)
            history = await load_context_messages(session_id, watermark)

            async def _on_summary(text: str) -> None:
                await session_turn_manager.publish(
                    turn_id, {"type": "summary", "text": text}
                )

            processor = make_history_processor(
                context_window=context_window,
                watermark_at_turn_start=start_seq,
                state=summary_state,
                on_summary=_on_summary,
            )

            # Instructions (not system_prompt) so the prompt is applied fresh each
            # turn and NEVER persisted into message_history — avoids a duplicated
            # system prompt accumulating across a multi-turn session.
            agent = Agent(
                build_model(settings, model_name),
                toolsets=[toolset],
                tools=[schedule_tool],
                instructions=SYSTEM_PROMPT,
                capabilities=[ProcessHistory(processor)],
                name="funnel-runtime-agent",
            )
            limits = UsageLimits(
                request_limit=settings.run_request_limit,
                tool_calls_limit=settings.run_tool_calls_limit,
            )

            usage: dict[str, Any] = {}
            new_messages: list = []
            async with asyncio.timeout(settings.run_timeout_seconds) as timeout_cm:
                handle.timeout_cm = timeout_cm
                async with agent:  # opens the MCP toolset connection
                    async with agent.iter(
                        user_message, message_history=history, usage_limits=limits
                    ) as agent_run:
                        async for node in agent_run:
                            await self._checkpoint(handle)
                            if Agent.is_model_request_node(node):
                                await self._stream_model_request(handle, agent_run, node)
                            elif Agent.is_call_tools_node(node):
                                await self._stream_tool_calls(handle, agent_run, node)
                    result = agent_run.result
                    usage = _usage_dict(agent_run.usage)
                    if result is not None:
                        new_messages = list(result.new_messages())

            # Persist the turn's verbatim messages (+ any fired summary) and the
            # usage snapshot on the final assistant row.
            await persist_turn(
                session_id=session_id,
                turn_id=turn_id,
                start_seq=start_seq,
                new_messages=new_messages,
                model=model_name,
                usage=usage or None,
                summary_text=summary_state.summary_text,
            )
            if summary_state.summary_text:
                # Everything before this turn is now represented by the summary row.
                await self._advance_watermark(session_id, start_seq)
            await self._touch_session(session_id, clear_error=True)

            # Emit usage + the terminal turn_complete, then close the buffer.
            usage_event = dict(usage)
            usage_event["cost"] = estimate_cost(
                model_name, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            )
            await session_turn_manager.publish(turn_id, {"type": "usage", **usage_event})
            await session_turn_manager.publish(
                turn_id, {"type": "turn_complete", "usage": usage_event}
            )
            await session_turn_manager.finish(turn_id, status=TurnStatus.COMPLETE)
            await publish_job(
                job_id=turn_id, ctx=handle.ctx, status=JobStatus.COMPLETED, progress=1.0,
                exit_status="ok", meta={"session_id": session_id},
            )
        except _Canceled:
            await self._fail_turn(handle, "canceled by control request", JobStatus.CANCELED, "canceled")
        except (asyncio.TimeoutError, TimeoutError):
            await self._fail_turn(
                handle, f"turn exceeded time limit ({settings.run_timeout_seconds}s)",
                JobStatus.FAILED, "timeout",
            )
        except UsageLimitExceeded as exc:
            await self._fail_turn(handle, f"usage limit reached: {exc}", JobStatus.FAILED, "usage_limit")
        except asyncio.CancelledError:
            await self._fail_turn(handle, "service shutting down", JobStatus.FAILED, "shutdown")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent turn %s failed", turn_id)
            await self._fail_turn(handle, str(exc), JobStatus.FAILED, "error")
        finally:
            otel_context.detach(otel_reset)
            try:
                await expire_pending_for_turn(session_id, turn_id)
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                logger.exception("agent turn %s: failed to expire pending approvals", turn_id)
            for approval_id in list(handle.approval_ids):
                approval_coordinator.discard(approval_id)
            handle.approval_ids.clear()
            self._turns.pop(turn_id, None)

    async def _fail_turn(
        self, handle: TurnHandle, detail: str, status: JobStatus, exit_status: str
    ) -> None:
        """Terminal-failure path: emit an in-stream error (NEVER raise — P8), close
        the buffer, publish the terminal job event, and remember the error on the
        session so the derived status can show it after the buffer expires."""
        turn_id, session_id = handle.turn_id, handle.session_id
        try:
            await session_turn_manager.publish(turn_id, {"type": "error", "detail": detail})
            await session_turn_manager.finish(turn_id, status=TurnStatus.ERROR)
        except Exception:  # noqa: BLE001
            logger.debug("agent turn %s: error-event publish failed", turn_id, exc_info=True)
        # Persist the user's own message even though the turn failed, so the input
        # (and the next turn's memory of it) isn't lost — persist_turn only ran on
        # the success path. Best-effort; no-op if the turn died before it was seq'd.
        try:
            await persist_failed_user_message(
                session_id=session_id,
                turn_id=turn_id,
                start_seq=handle.start_seq,
                user_message=handle.user_message,
                model=handle.model_name,
            )
        except Exception:  # noqa: BLE001
            logger.debug("agent turn %s: failed-turn user-message persist failed", turn_id, exc_info=True)
        try:
            # A canceled turn is NOT an error — clear any stale last_error so the
            # session's derived status reflects the latest turn, not a prior failure.
            await self._touch_session(
                session_id,
                error=detail if status is JobStatus.FAILED else None,
                clear_error=status is not JobStatus.FAILED,
            )
        except Exception:  # noqa: BLE001
            logger.debug("agent turn %s: session touch failed", turn_id, exc_info=True)
        await publish_job(
            job_id=turn_id, ctx=handle.ctx, status=status, exit_status=exit_status,
            meta={"session_id": session_id},
        )

    async def _checkpoint(self, handle: TurnHandle) -> None:
        """Between graph nodes: honor cancel, apply a pause."""
        if handle.cancel_event.is_set():
            raise _Canceled()
        if handle.paused:
            await publish_job(
                job_id=handle.turn_id, ctx=handle.ctx, status=JobStatus.PAUSED,
                meta={"session_id": handle.session_id},
            )
            await handle.resume_event.wait()
            if handle.cancel_event.is_set():
                raise _Canceled()
            await publish_job(
                job_id=handle.turn_id, ctx=handle.ctx, status=JobStatus.RUNNING,
                meta={"session_id": handle.session_id},
            )

    async def _stream_model_request(self, handle: TurnHandle, agent_run, node) -> None:
        """Stream a model-request node's text/thinking deltas to the buffer."""
        turn_id = handle.turn_id
        async with node.stream(agent_run.ctx) as request_stream:
            async for event in request_stream:
                if isinstance(event, PartStartEvent):
                    part = event.part
                    if isinstance(part, TextPart) and part.content:
                        await session_turn_manager.publish(
                            turn_id, {"type": "text_delta", "text": part.content}
                        )
                    elif isinstance(part, ThinkingPart) and part.content:
                        await session_turn_manager.publish(
                            turn_id, {"type": "thinking_delta", "text": part.content}
                        )
                elif isinstance(event, PartDeltaEvent):
                    delta = event.delta
                    if isinstance(delta, TextPartDelta) and delta.content_delta:
                        await session_turn_manager.publish(
                            turn_id, {"type": "text_delta", "text": delta.content_delta}
                        )
                    elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                        await session_turn_manager.publish(
                            turn_id, {"type": "thinking_delta", "text": delta.content_delta}
                        )

    async def _stream_tool_calls(self, handle: TurnHandle, agent_run, node) -> None:
        """Stream a call-tools node's tool_call / tool_result events to the buffer."""
        turn_id = handle.turn_id
        async with node.stream(agent_run.ctx) as handle_stream:
            async for event in handle_stream:
                if isinstance(event, FunctionToolCallEvent):
                    part = event.part
                    try:
                        args = part.args_as_dict()
                    except Exception:  # noqa: BLE001
                        args = {}
                    await session_turn_manager.publish(
                        turn_id,
                        {"type": "tool_call", "id": part.tool_call_id,
                         "name": part.tool_name, "args": args},
                    )
                elif isinstance(event, FunctionToolResultEvent):
                    rp = event.part
                    await session_turn_manager.publish(
                        turn_id,
                        {"type": "tool_result", "id": getattr(rp, "tool_call_id", ""),
                         "name": getattr(rp, "tool_name", ""),
                         "content": _jsonable(getattr(rp, "content", None))},
                    )

    # --- Principle-4 human-approval gate ----------------------------------

    def _make_process_tool_call(self, handle: TurnHandle):
        """Build the pydantic-ai ``process_tool_call`` hook for this turn.

        Wraps every MCP tool call: run it, and if the result is the fm_runtime
        agent-approval gate (``needs_human_approval`` — a structured RESULT, never
        a raised error), hand off to the approval flow. The gate is intercepted
        HERE, before any tool-error branch, so it can never be retried past the
        human (fix #31)."""

        async def process_tool_call(ctx, call_tool, name: str, tool_args: dict[str, Any]):
            result = await call_tool(name, tool_args)
            gate = _extract_approval_gate(result)
            if gate is None:
                return result
            return await self._await_approval(handle, call_tool, name, tool_args, gate)

        return process_tool_call

    async def _await_approval(
        self, handle: TurnHandle, call_tool, name: str, tool_args: dict[str, Any],
        gate: dict[str, Any],
    ) -> Any:
        """Pause the turn on an over-threshold agent action until the initiating
        human decides, then re-issue (approved) or report the rejection."""
        turn_id, session_id = handle.turn_id, handle.session_id
        subject = handle.ctx.user  # initiating human == session owner
        estimate = float(gate.get("estimate") or 0.0)
        action = str(gate.get("action") or "")
        threshold = gate.get("threshold")
        unit = str(gate.get("unit") or "")
        message = str(gate.get("message") or "")
        # Trust our own recomputed ref (bound to exactly this subject).
        ref = make_approval_ref(action, estimate, subject)

        approval_id = uuid.uuid4().hex
        # Register the waiter BEFORE persisting the row so a human can never
        # resolve an approval the turn is not yet awaiting.
        approval_coordinator.register(approval_id)
        handle.approval_ids.add(approval_id)
        await create_pending_approval(
            approval_id=approval_id,
            session_id=session_id,
            turn_id=turn_id,
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
            "agent turn %s paused for human approval: action=%s estimate=%s%s ref=%s",
            turn_id, action, estimate, (" " + unit) if unit else "", ref,
        )

        # Surface the approval to the chat stream AND to the jobs producer, and
        # freeze the wall-clock deadline (a human may take a while).
        await handle.suspend_timeout()
        await session_turn_manager.publish(
            turn_id,
            {"type": "approval_required", "approval_id": approval_id, "action": action,
             "estimate": estimate,
             "threshold": float(threshold) if isinstance(threshold, (int, float)) else None,
             "unit": unit, "message": message, "tool_name": name},
        )
        approval_meta = {
            "awaiting_approval": True, "approval_id": approval_id, "approval_ref": ref,
            "action": action, "estimate": estimate, "session_id": session_id,
        }
        await publish_job(
            job_id=turn_id, ctx=handle.ctx, status=JobStatus.PAUSED, meta=approval_meta
        )

        try:
            decision = await approval_coordinator.wait(approval_id, handle.cancel_event)
        finally:
            await handle.resume_timeout()
            approval_coordinator.discard(approval_id)

        if decision is None:
            # Canceled while waiting: abandon this call cleanly (no raise from
            # inside a tool call, which pydantic-ai might wrap/retry).
            return {
                "error": "canceled",
                "message": "The turn was canceled while awaiting human approval; "
                "this action was not performed.",
                "action": action,
            }

        await publish_job(
            job_id=turn_id, ctx=handle.ctx, status=JobStatus.RUNNING,
            meta={"approval_id": approval_id, "approved": decision.approved,
                  "session_id": session_id},
        )

        if not decision.approved:
            return {
                "error": "human_rejected",
                "message": "A human reviewed this expensive action and REJECTED it. "
                "Do not retry it; continue with the rest of the task or stop.",
                "action": action,
            }

        # Single-use guard: if the ref was spent between approval and now, do NOT
        # re-issue (the still-valid token would replay for a second action).
        if await is_ref_consumed(ref):
            return {
                "error": "approval_spent",
                "message": "This action's human approval was already used for "
                "another action and cannot be reused; do not retry it.",
                "action": action,
            }

        # Approved: re-issue the EXACT call with the human-minted token injected
        # server-side (never by the LLM). Mark the ref consumed once the gate
        # actually lets it through (result is no longer a gate) — single-use.
        approved_args = {**tool_args, "human_approval": decision.token}
        reissued = await call_tool(name, approved_args)
        if _extract_approval_gate(reissued) is None:
            await mark_ref_consumed(
                approval_ref=ref, session_id=session_id, turn_id=turn_id,
                approval_id=approval_id, subject=subject, action=action, estimate=estimate,
            )
        return reissued

    # --- persistence ------------------------------------------------------

    async def _session_watermark(self, session_id: str) -> int:
        async with SessionLocal() as db:
            sess = await db.get(AgentSession, session_id)
            return int(sess.context_watermark) if sess is not None else 0

    async def _advance_watermark(self, session_id: str, watermark: int) -> None:
        async with SessionLocal() as db:
            sess = await db.get(AgentSession, session_id)
            if sess is not None:
                sess.context_watermark = int(watermark)
                await db.commit()

    async def _touch_session(
        self, session_id: str, *, error: str | None = None, clear_error: bool = False
    ) -> None:
        async with SessionLocal() as db:
            sess = await db.get(AgentSession, session_id)
            if sess is not None:
                sess.updated_at = _utcnow()
                if clear_error:
                    sess.last_error = None
                elif error is not None:
                    sess.last_error = error[:2000]
                await db.commit()


def agents_actor() -> str:
    """The agents service's own OAuth client id (``azp`` it acts under) — recorded
    as a session's ``actor`` so it renders "alice (via agent)"."""
    return get_runtime_settings().effective_client_id or "agents"


turn_runner = TurnRunner()

__all__ = ["TurnRunner", "agents_actor", "build_model", "turn_runner"]
