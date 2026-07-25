"""Request/response contracts for the agents API.

This is the API contract the ``agentsui`` frontend (owned by ``agentsui-agent``)
codes against — kept stable/additive. Runtime-agent runs are cross-user visible
(principle 1): the list/detail views expose every run's owner but never hide a
run from another ``agents-access`` user.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class TaskDetail(TaskSummary):
    params: dict[str, Any] = Field(default_factory=dict)
    result: str | None = None
    error: str | None = None
    steps: int = 0
    usage: dict[str, Any] | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
