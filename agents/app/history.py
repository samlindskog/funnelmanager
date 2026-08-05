"""Verbatim message persistence + context (history) management.

Two concerns, kept out of the runner so it stays orchestration-only:

1. **Verbatim store** — every pydantic-ai ``ModelMessage`` a turn produces is
   serialized via ``ModelMessagesTypeAdapter`` and appended as an ``AgentMessage``
   row (the source of truth for replay/display AND the next turn's model context).
   Reconstruction is the exact inverse (deserialize each row's ``content``).

2. **Context management** — a ``ProcessHistory`` capability condenses the in-run
   message list when it nears the model's max context, so a long conversation
   never overruns the window. When it fires it stashes a summary + a watermark on
   the turn handle; the runner persists the summary row (kind ``summary``) and
   advances ``AgentSession.context_watermark`` at turn end (one place assigns
   seqs, so no mid-turn seq collisions). The summary is a **deterministic digest**
   (no extra mid-run LLM call — that would add cost, latency, and re-entrancy
   risk); an LLM-backed summarizer is a future refinement. Tool-call/tool-result
   pairs are never split across the trim.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    MSG_KIND_ASSISTANT,
    MSG_KIND_SUMMARY,
    MSG_KIND_TOOL_CALL,
    MSG_KIND_TOOL_RESULT,
    MSG_KIND_USER,
    AgentMessage,
)

logger = logging.getLogger(__name__)

# Summarize when the running context reaches this fraction of the model's window.
_CONTEXT_TRIGGER_FRACTION = 0.75
# Always keep at least this many of the most-recent messages verbatim.
_KEEP_RECENT_MIN = 6


# --- serialization ----------------------------------------------------------


def serialize_messages(messages: list[ModelMessage]) -> list[dict]:
    """Serialize ModelMessages to plain JSON-able dicts (canonical adapter form)."""
    return list(ModelMessagesTypeAdapter.dump_python(messages, mode="json"))


def deserialize_messages(rows: list[dict]) -> list[ModelMessage]:
    """Inverse of :func:`serialize_messages`."""
    if not rows:
        return []
    return list(ModelMessagesTypeAdapter.validate_python(rows))


def message_kind(msg: ModelMessage) -> str:
    """Coarse display label for a ModelMessage (content dict stays the truth)."""
    if isinstance(msg, ModelResponse):
        has_tool = any(isinstance(p, ToolCallPart) for p in msg.parts)
        has_text = any(isinstance(p, (TextPart, ThinkingPart)) and getattr(p, "content", "") for p in msg.parts)
        return MSG_KIND_TOOL_CALL if (has_tool and not has_text) else MSG_KIND_ASSISTANT
    # ModelRequest
    parts = getattr(msg, "parts", [])
    if any(isinstance(p, UserPromptPart) for p in parts):
        return MSG_KIND_USER
    if any(isinstance(p, (ToolReturnPart, RetryPromptPart)) for p in parts):
        return MSG_KIND_TOOL_RESULT
    return MSG_KIND_USER


# --- verbatim persistence ---------------------------------------------------


async def next_seq(session_id: str) -> int:
    """Current max seq for a session (0 if none)."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(func.coalesce(func.max(AgentMessage.seq), 0)).where(
                AgentMessage.session_id == session_id
            )
        )
        return int(result.scalar() or 0)


async def load_context_messages(session_id: str, watermark: int) -> list[ModelMessage]:
    """Rebuild the model context for the next turn.

    = the latest ``summary`` row (representing everything folded up to
    ``watermark``) + every verbatim non-summary row with ``seq > watermark``.
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.seq.asc())
            )
        ).scalars().all()

    context_dicts: list[dict] = []
    if watermark > 0:
        summaries = [r for r in rows if r.kind == MSG_KIND_SUMMARY]
        if summaries:
            context_dicts.append(summaries[-1].content)
    context_dicts.extend(
        r.content for r in rows if r.seq > watermark and r.kind != MSG_KIND_SUMMARY
    )
    try:
        return deserialize_messages(context_dicts)
    except Exception:  # noqa: BLE001 — a corrupt row must not wedge the whole turn
        logger.exception("agents: failed to deserialize context for session %s", session_id)
        return []


async def persist_turn(
    *,
    session_id: str,
    turn_id: str,
    start_seq: int,
    new_messages: list[ModelMessage],
    model: str,
    usage: dict[str, Any] | None,
    summary_text: str | None,
) -> int:
    """Append a turn's verbatim messages (and an optional leading summary row).

    Seqs are assigned here (the single writer) starting after ``start_seq``. The
    summary row, if any, is written FIRST so the next turn's context load reads it
    as "everything before this turn". ``usage`` is stamped on the final
    assistant/tool row. Returns the new max seq.
    """
    seq = start_seq
    async with SessionLocal() as db:
        if summary_text:
            seq += 1
            summary_msg = ModelRequest(parts=[UserPromptPart(content=summary_text)])
            db.add(
                AgentMessage(
                    id=_row_id(session_id, seq),
                    session_id=session_id,
                    seq=seq,
                    turn_id=turn_id,
                    kind=MSG_KIND_SUMMARY,
                    content=serialize_messages([summary_msg])[0],
                    model=model,
                )
            )

        serialized = serialize_messages(new_messages)
        # Index of the last assistant/response row, to carry the usage snapshot.
        last_assistant_idx = _last_response_index(new_messages)
        for idx, (msg, dumped) in enumerate(zip(new_messages, serialized)):
            seq += 1
            kind = message_kind(msg)
            row_usage = usage if idx == last_assistant_idx else None
            row_model = model if isinstance(msg, ModelResponse) else None
            db.add(
                AgentMessage(
                    id=_row_id(session_id, seq),
                    session_id=session_id,
                    seq=seq,
                    turn_id=turn_id,
                    kind=kind,
                    content=dumped,
                    model=row_model,
                    usage=row_usage,
                )
            )
        await db.commit()
    return seq


def _last_response_index(messages: list[ModelMessage]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], ModelResponse):
            return i
    return -1


def _row_id(session_id: str, seq: int) -> str:
    return f"{session_id}:{seq}"


# --- context (history) processor -------------------------------------------


def _all_tool_returns(msg: ModelMessage) -> bool:
    """True if a ModelRequest consists solely of tool-return/retry parts (would be
    orphaned if it started a trimmed tail)."""
    if not isinstance(msg, ModelRequest):
        return False
    parts = getattr(msg, "parts", [])
    return bool(parts) and all(isinstance(p, (ToolReturnPart, RetryPromptPart)) for p in parts)


def _digest(messages: list[ModelMessage]) -> str:
    """A compact deterministic recap of trimmed messages (no LLM call)."""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for p in msg.parts:
                if isinstance(p, TextPart) and p.content:
                    lines.append(f"assistant: {p.content.strip()[:300]}")
                elif isinstance(p, ToolCallPart):
                    lines.append(f"assistant called tool {p.tool_name}")
        elif isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, UserPromptPart):
                    content = p.content if isinstance(p.content, str) else str(p.content)
                    lines.append(f"user: {content.strip()[:300]}")
                elif isinstance(p, ToolReturnPart):
                    lines.append(f"tool {p.tool_name} returned a result")
    body = "\n".join(lines[-60:])  # bound the recap length
    return "[Earlier conversation summary]\n" + body


class _SummaryState:
    """Mutable sink the runner reads after a turn to persist a fired summary."""

    def __init__(self) -> None:
        self.summary_text: str | None = None
        self.fired: bool = False


def make_history_processor(
    *,
    context_window: int,
    watermark_at_turn_start: int,
    state: _SummaryState,
    on_summary,  # callable(text) -> awaitable, emits the `summary` stream event
):
    """Build the async ``ProcessHistory`` processor for a turn.

    Condenses the in-run message list when ``ctx.usage.total_tokens`` reaches
    ``_CONTEXT_TRIGGER_FRACTION`` of ``context_window``: replaces the older prefix
    with one summary message, keeping the recent tail verbatim and never splitting
    a tool-call/tool-result pair. Records the summary + fires the stream event
    once; the runner persists the row and advances the watermark at turn end.
    """
    trigger = max(1, int(context_window * _CONTEXT_TRIGGER_FRACTION))

    async def processor(ctx, messages: list[ModelMessage]) -> list[ModelMessage]:
        total = int(getattr(ctx.usage, "total_tokens", 0) or 0)
        if state.fired or total < trigger or len(messages) <= _KEEP_RECENT_MIN:
            return messages

        # Keep the most-recent tail; summarize the rest. Nudge the boundary earlier
        # so the tail never starts with orphaned tool-return parts.
        split = max(0, len(messages) - _KEEP_RECENT_MIN)
        while split > 0 and _all_tool_returns(messages[split]):
            split -= 1
        if split <= 0:
            return messages

        prefix, tail = messages[:split], messages[split:]
        summary_text = _digest(prefix)
        state.summary_text = summary_text
        state.fired = True
        try:
            await on_summary(summary_text)
        except Exception:  # noqa: BLE001 — a stream hiccup must not fail the turn
            logger.debug("agents: summary event publish failed", exc_info=True)
        summary_msg = ModelRequest(parts=[UserPromptPart(content=summary_text)])
        return [summary_msg, *tail]

    return processor


__all__ = [
    "deserialize_messages",
    "load_context_messages",
    "make_history_processor",
    "message_kind",
    "next_seq",
    "persist_turn",
    "serialize_messages",
    "_SummaryState",
]
