"""In-process fan-out + replay for a session's streamed turns.

Modeled on ``leads/app/stream_jobs.py`` ``StreamJobManager``: a **turn** (one user
message → the agent's streamed response) buffers every NDJSON event so a
subscriber that attaches late — or reattaches after the tab was closed — replays
the whole in-progress turn, then goes live. Finished turns stay resolvable for a
short grace window (~90s) so a reconnecting client finishes cleanly, then the
buffer is dropped to bound memory. Multiplex is round-robin-free here because a
session runs **one turn at a time** (a chat is serial); the substrate is per-turn.

Single-replica assumption (like the leads manager): the turn buffers live in this
process's memory — no distributed pub/sub. A pod restart loses an in-flight turn's
buffer (the plan accepts this; verbatim history + schedules survive in Postgres).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Keep a finished turn's buffer resolvable briefly for reattach, then drop it.
_POST_TURN_TTL_SECONDS = 90.0
# A turn whose runner never published (spawn failed) ages out on the same window.
_STALE_TURN_MAX_AGE_SECONDS = _POST_TURN_TTL_SECONDS


class TurnStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class TurnBuffer:
    turn_id: str
    session_id: str
    status: TurnStatus = TurnStatus.RUNNING
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    subscribers: int = 0
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    _cleanup_handle: asyncio.TimerHandle | None = field(default=None, repr=False)


class SessionTurnManager:
    """Owns the live per-turn event buffers and their expiry."""

    def __init__(self) -> None:
        self._turns: dict[str, TurnBuffer] = {}
        # session_id -> the turn_id currently running/paused (for reattach + the
        # "one turn at a time" guard). Cleared when the turn finishes.
        self._active: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start_turn(self, session_id: str, turn_id: str) -> TurnBuffer:
        """Register a new turn buffer. Rejects a second concurrent turn for the
        same session by raising ``RuntimeError`` (a chat is serial)."""
        async with self._lock:
            existing = self._active.get(session_id)
            if existing is not None and existing in self._turns:
                raise RuntimeError("a turn is already running for this session")
            buf = TurnBuffer(turn_id=turn_id, session_id=session_id)
            self._turns[turn_id] = buf
            self._active[session_id] = turn_id
        # Drop the buffer if the runner never publishes (spawn failure guard).
        self._schedule_expiry(buf, delay=_STALE_TURN_MAX_AGE_SECONDS)
        return buf

    def get(self, turn_id: str) -> TurnBuffer | None:
        return self._turns.get(turn_id)

    def active_turn_for_session(self, session_id: str) -> str | None:
        """The turn_id currently running/paused for a session, or None if idle."""
        turn_id = self._active.get(session_id)
        if turn_id is None:
            return None
        return turn_id if turn_id in self._turns else None

    def active_status(self, session_id: str) -> TurnStatus | None:
        """Live status of a session's active turn (None if idle/expired)."""
        turn_id = self._active.get(session_id)
        if turn_id is None:
            return None
        buf = self._turns.get(turn_id)
        return buf.status if buf is not None else None

    async def publish(self, turn_id: str, event: dict[str, Any]) -> None:
        buf = self._turns.get(turn_id)
        if buf is None:
            return
        # A status-bearing event keeps the live derived session status honest even
        # before a terminal event (e.g. approval_required -> paused).
        etype = event.get("type")
        async with buf._condition:
            buf.history.append(dict(event))
            if etype == "approval_required":
                buf.status = TurnStatus.PAUSED
            elif etype in ("text_delta", "thinking_delta", "tool_call", "tool_result"):
                if buf.status is TurnStatus.PAUSED:
                    buf.status = TurnStatus.RUNNING
            buf._condition.notify_all()

    async def finish(
        self, turn_id: str, *, status: TurnStatus = TurnStatus.COMPLETE
    ) -> None:
        buf = self._turns.get(turn_id)
        if buf is None:
            return
        async with buf._condition:
            buf.status = status
            buf.finished_at = time.monotonic()
            buf._condition.notify_all()
        # Free the session for a new turn immediately; the buffer lingers for replay.
        async with self._lock:
            if self._active.get(buf.session_id) == turn_id:
                self._active.pop(buf.session_id, None)
        self._schedule_expiry(buf)

    async def subscribe(self, turn_id: str) -> AsyncIterator[dict[str, Any]]:
        """Replay the turn's buffered events, then stream live ones until terminal.

        Yields an ``error`` line for an unknown/expired turn (never raises), so the
        NDJSON route can forward it as a normal stream line (P8)."""
        buf = self._turns.get(turn_id)
        if buf is None:
            yield {"type": "error", "detail": "unknown or expired turn"}
            return

        buf.subscribers += 1
        cursor = 0
        try:
            while True:
                async with buf._condition:
                    while cursor >= len(buf.history) and buf.finished_at is None:
                        # Bounded wait so a wedged producer can't hang the client
                        # forever; loop re-checks history + terminal state.
                        try:
                            await asyncio.wait_for(buf._condition.wait(), timeout=30.0)
                        except asyncio.TimeoutError:
                            break
                    pending = buf.history[cursor:]
                    cursor = len(buf.history)
                    finished = buf.finished_at is not None
                for event in pending:
                    yield event
                if finished and cursor >= len(buf.history):
                    return
        finally:
            buf.subscribers = max(0, buf.subscribers - 1)
            if buf.subscribers == 0 and buf.finished_at is not None:
                self._schedule_expiry(buf)

    # --- expiry (mirrors leads' TTL bookkeeping) --------------------------

    def _schedule_expiry(self, buf: TurnBuffer, *, delay: float | None = None) -> None:
        now = time.monotonic()
        if delay is None:
            if buf.finished_at is not None:
                delay = max(0.0, _POST_TURN_TTL_SECONDS - (now - buf.finished_at))
            else:
                delay = max(0.0, _STALE_TURN_MAX_AGE_SECONDS - (now - buf.created_at))
        if buf._cleanup_handle is not None:
            buf._cleanup_handle.cancel()
            buf._cleanup_handle = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _fire() -> None:
            buf._cleanup_handle = None
            asyncio.create_task(self._expire(buf.turn_id))

        buf._cleanup_handle = loop.call_later(delay, _fire)

    async def _expire(self, turn_id: str) -> None:
        buf = self._turns.get(turn_id)
        if buf is None:
            return
        now = time.monotonic()
        if buf.subscribers > 0:
            self._schedule_expiry(buf, delay=_POST_TURN_TTL_SECONDS)
            return
        if buf.finished_at is None:
            # Runner never published and the buffer is stale — drop it.
            if now - buf.created_at < _STALE_TURN_MAX_AGE_SECONDS:
                self._schedule_expiry(buf)
                return
        elif now - buf.finished_at < _POST_TURN_TTL_SECONDS:
            self._schedule_expiry(buf)
            return
        self._drop(buf)

    def _drop(self, buf: TurnBuffer) -> None:
        if buf._cleanup_handle is not None:
            buf._cleanup_handle.cancel()
            buf._cleanup_handle = None
        buf.history.clear()
        self._turns.pop(buf.turn_id, None)
        async_active = self._active.get(buf.session_id)
        if async_active == buf.turn_id:
            self._active.pop(buf.session_id, None)


session_turn_manager = SessionTurnManager()

__all__ = ["SessionTurnManager", "TurnBuffer", "TurnStatus", "session_turn_manager"]
