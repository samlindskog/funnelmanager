"""Request/response contracts for the agents API.

This is the API contract the ``agentsui`` frontend (owned by ``agentsui-agent``)
codes against — kept stable/additive. Runtime-agent runs are cross-user visible
(principle 1): the list/detail views expose every run's owner but never hide a
run from another ``agents-access`` user.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    username: str
    role: str = ""


class CreateTaskRequest(BaseModel):
    """Start a runtime-agent run from a natural-language goal + optional params.

    ``params`` is free-form structured context handed to the agent (e.g. a
    search title, a page size); the agent still decides which MCP tools to call.
    """

    goal: str = Field(min_length=1, max_length=8000)
    params: dict[str, Any] = Field(default_factory=dict)


class TaskSummary(BaseModel):
    id: str
    goal: str
    status: str
    owner: str
    origin: str
    actor: str
    progress: float | None = None
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


class PendingApprovalOut(BaseModel):
    """A Principle-4 pending human approval surfaced on a run's detail view.

    An over-threshold action the runtime agent attempted (e.g. launching a large
    campaign) that it cannot self-confirm — it is waiting for the initiating human
    to approve or reject via ``POST /api/agents/tasks/{id}/approvals/{aid}``.
    ``agentsui`` renders these and drives the decision. Read cross-user, but only
    the run's ``owner`` may decide (enforced server-side)."""

    id: str
    run_id: str
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


class ApprovalDecisionRequest(BaseModel):
    """Approve or reject a pending approval. ``approve`` lets the SAME action
    re-run with a human-minted token; ``reject`` skips it (the run continues or
    stops per policy)."""

    decision: Literal["approve", "reject"]


class TaskDetail(TaskSummary):
    params: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    error: str | None = None
    steps: int = 0
    usage: dict[str, Any] | None = None
    # Actionable Principle-4 approvals blocking this run (empty when none pending).
    pending_approvals: list[PendingApprovalOut] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    """Outcome of an approve/reject on a pending approval."""

    approval: PendingApprovalOut
    run_status: str
    resumed: bool


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
