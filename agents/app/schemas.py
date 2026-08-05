"""Request/response contracts for the agents **sessions** API.

This is the API contract ``agentsui`` codes against (Phase 4 rebuilds the UI onto
it) — kept stable/additive. Sessions are cross-user visible (Principle 1): list
and detail expose every session's owner but never hide a session from another
``agents-access`` user. The one per-user carve-out is destructive ownership
(delete-my-own-session), enforced as a 404 in the router — not a read filter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    username: str


# --- sessions ---------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Create a chat session. ``title`` auto-derives from the first user message
    if omitted; ``model`` defaults to the service's configured model."""

    title: str | None = Field(default=None, max_length=400)
    model: str | None = Field(default=None, max_length=128)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=400)


class SessionSummary(BaseModel):
    id: str
    title: str
    model: str
    owner: str
    origin: str
    actor: str
    # Derived at read time (running/paused/scheduled/error/idle) — never stored.
    status: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """One verbatim transcript message. ``content`` is the serialized ModelMessage
    (source of truth); ``kind`` is a coarse display label."""

    id: str
    seq: int
    turn_id: str
    kind: str
    content: dict[str, Any]
    model: str | None = None
    usage: dict[str, Any] | None = None
    created_at: datetime


class PendingApprovalOut(BaseModel):
    """A Principle-4 pending human approval surfaced on a session's detail view.

    An over-threshold action the agent attempted that it cannot self-confirm,
    waiting for the initiating human to approve/reject via
    ``POST /api/agents/sessions/{id}/approvals/{aid}``. Read cross-user, but only
    the session ``owner`` may decide (enforced server-side)."""

    id: str
    session_id: str
    turn_id: str
    subject: str
    approval_ref: str
    action: str
    estimate: float
    threshold: float | None = None
    unit: str = ""
    message: str = ""
    tool_name: str = ""
    status: str
    created_at: datetime


class SessionDetail(SessionSummary):
    context_watermark: int = 0
    last_error: str | None = None
    messages: list[MessageOut] = Field(default_factory=list)
    pending_approvals: list[PendingApprovalOut] = Field(default_factory=list)


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    # Distinct owners across ALL sessions (not just this page) so the UI can build
    # the cross-user owner picker (Principle 1: attributed, not hidden).
    owners: list[str] = Field(default_factory=list)


# --- turns / messages -------------------------------------------------------


class PostMessageRequest(BaseModel):
    """Send a user message, streaming the resulting turn as NDJSON."""

    content: str = Field(min_length=1, max_length=32000)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ApprovalDecisionResponse(BaseModel):
    approval: PendingApprovalOut
    resumed: bool


# --- models + stats ---------------------------------------------------------


class ModelInfo(BaseModel):
    id: str
    context_window: int
    owned_by: str = "openai"


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    default: str


class ModelUsageStat(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    requests: int
    tool_calls: int
    turns: int
    cost: float | None = None


class StatsResponse(BaseModel):
    by_model: list[ModelUsageStat]
    total_cost: float | None = None
