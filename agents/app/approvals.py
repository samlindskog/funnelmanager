"""Principle-4 pending-approval store + in-process resume coordinator.

Hard enforcement: a runtime AI agent **can never self-confirm** an over-threshold
action. When an MCP tool call returns the fm_runtime agent-approval gate
(``needs_human_approval``), the running **turn** pauses and records a
:class:`~app.models.PendingApproval`; the ONLY way the action proceeds is the
*initiating human* approving it out-of-band, after which the ``agents`` service
mints an unforgeable ``human_approval`` token and resumes the turn so it re-issues
the exact tool call WITH the approval.

Re-keyed from the retired run model to ``(session_id, turn_id)`` — the paused
unit is now a turn within a session. Two halves:

- **Persistence** (this store) — the durable record surfaced to ``agentsui`` and
  decided via the approvals API.
- **Coordinator** (:class:`ApprovalCoordinator`) — the in-process rendezvous
  between the API request recording a human's decision and the paused turn task
  awaiting it (both live in this service's event loop). The minted token rides
  **only** this in-memory channel — never the DB — so a persisted row can never
  leak a bearer-equivalent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import (
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    ConsumedApproval,
    PendingApproval,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ApprovalDecision:
    """A human's decision on a pending approval, delivered to the paused turn.

    ``approved`` carries the freshly-minted ``human_approval`` token (injected
    into the re-issued tool call server-side — the LLM never sees it). A rejection
    carries no token; the turn surfaces the rejection to the model and continues
    or stops per policy.
    """

    approved: bool
    token: str | None = None
    decided_by: str = ""


@dataclass
class _Waiter:
    event: asyncio.Event
    decision: ApprovalDecision | None = None


class ApprovalCoordinator:
    """In-process rendezvous: the approvals API resolves a waiter the paused turn
    task is awaiting. Both run in this service's event loop (single-process);
    this is not a cross-process queue."""

    def __init__(self) -> None:
        self._waiters: dict[str, _Waiter] = {}

    def register(self, approval_id: str) -> None:
        """Create the waiter BEFORE the pending-approval row is persisted, so a
        human can never resolve an approval the turn isn't yet waiting on."""
        self._waiters[approval_id] = _Waiter(event=asyncio.Event())

    def is_waiting(self, approval_id: str) -> bool:
        w = self._waiters.get(approval_id)
        return w is not None and not w.event.is_set()

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """Deliver a decision to the paused turn. Returns False if there is no live
        waiter (the turn ended/moved on) or it was already decided."""
        w = self._waiters.get(approval_id)
        if w is None or w.event.is_set():
            return False
        w.decision = decision
        w.event.set()
        return True

    async def wait(
        self, approval_id: str, cancel_event: asyncio.Event
    ) -> ApprovalDecision | None:
        """Block the turn until the human decides, or the turn is canceled.

        Returns the :class:`ApprovalDecision` on a decision, or ``None`` if the
        turn was canceled while waiting (the tool call is then abandoned)."""
        w = self._waiters.get(approval_id)
        if w is None:
            return None
        decided = asyncio.ensure_future(w.event.wait())
        canceled = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait(
                {decided, canceled}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (decided, canceled):
                if not task.done():
                    task.cancel()
        if w.event.is_set():
            return w.decision
        return None

    def discard(self, approval_id: str) -> None:
        self._waiters.pop(approval_id, None)


# Module singleton — shared by the turn tasks (which await) and the approvals API
# (which resolves), both in this process.
approval_coordinator = ApprovalCoordinator()


# --- persistence helpers ---------------------------------------------------


async def create_pending_approval(
    *,
    approval_id: str,
    session_id: str,
    turn_id: str,
    subject: str,
    approval_ref: str,
    action: str,
    estimate: float,
    threshold: float | None,
    unit: str,
    message: str,
    tool_name: str,
    tool_args: dict,
) -> None:
    """Persist a pending approval for the turn. The minted token is never stored."""
    async with SessionLocal() as session:
        session.add(
            PendingApproval(
                id=approval_id,
                session_id=session_id,
                turn_id=turn_id,
                subject=subject,
                approval_ref=approval_ref,
                action=action,
                estimate=float(estimate),
                threshold=threshold,
                unit=unit,
                message=message,
                tool_name=tool_name,
                tool_args=dict(tool_args or {}),
                status=APPROVAL_PENDING,
            )
        )
        await session.commit()


async def get_pending_approval(
    session: AsyncSession, approval_id: str
) -> PendingApproval | None:
    return await session.get(PendingApproval, approval_id)


async def mark_decided(approval_id: str, *, status: str, decided_by: str) -> None:
    """Record the human's decision (approved/rejected) on the row."""
    async with SessionLocal() as session:
        row = await session.get(PendingApproval, approval_id)
        if row is not None and row.status == APPROVAL_PENDING:
            row.status = status
            row.decided_by = decided_by
            row.decided_at = _utcnow()
            await session.commit()


# --- single-use (consumed approval_ref) ledger -----------------------------
# The minted human_approval token is replayable within its TTL for the same
# action+estimate+subject; these two helpers make an approval one-shot.
# `is_ref_consumed` gates minting/re-issue; `mark_ref_consumed` records a spend
# atomically (PK on approval_ref → the first spend wins).


async def is_ref_consumed(approval_ref: str) -> bool:
    """True if this ``approval_ref`` was already spent by a completed action.

    Callers treat True as "block": refuse to mint a second token / re-issue for a
    ref already consumed, so one human approval can never authorize a replayed
    second over-threshold action."""
    if not approval_ref:
        return False
    async with SessionLocal() as session:
        return (await session.get(ConsumedApproval, approval_ref)) is not None


async def mark_ref_consumed(
    *,
    approval_ref: str,
    session_id: str,
    turn_id: str,
    approval_id: str,
    subject: str,
    action: str,
    estimate: float,
) -> bool:
    """Record an ``approval_ref`` as spent. Returns True if this call recorded the
    spend, False if the ref was already consumed (a concurrent/duplicate spend).

    The PK on ``approval_ref`` makes this an atomic check-and-set: two actions can
    never both believe they were the first to spend a ref. Idempotent."""
    if not approval_ref:
        return False
    async with SessionLocal() as session:
        session.add(
            ConsumedApproval(
                approval_ref=approval_ref,
                session_id=session_id,
                turn_id=turn_id,
                approval_id=approval_id,
                subject=subject,
                action=action,
                estimate=float(estimate),
            )
        )
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def expire_pending_for_turn(session_id: str, turn_id: str) -> None:
    """Mark any still-pending approvals for a turn as expired.

    Called when a turn reaches a terminal state with approvals left undecided —
    they can never be acted on again, so they must not linger as actionable
    ``pending`` rows in the UI."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PendingApproval).where(
                    PendingApproval.session_id == session_id,
                    PendingApproval.turn_id == turn_id,
                    PendingApproval.status == APPROVAL_PENDING,
                )
            )
        ).scalars().all()
        for row in rows:
            row.status = APPROVAL_EXPIRED
        if rows:
            await session.commit()


__all__ = [
    "ApprovalCoordinator",
    "ApprovalDecision",
    "approval_coordinator",
    "create_pending_approval",
    "expire_pending_for_turn",
    "get_pending_approval",
    "is_ref_consumed",
    "mark_decided",
    "mark_ref_consumed",
]
