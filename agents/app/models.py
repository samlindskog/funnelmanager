"""ORM models for the agents service.

An ``AgentRun`` is one runtime-AI-agent task: a human's goal, the pydantic-ai
loop that pursued it exclusively through MCP tools, and its lifecycle. Each run
is also a **job** (agents is a v1 jobs producer) — ``id`` is the job id the
``jobs`` service stores, and the attribution columns are exactly the job-event
fields so a run renders as "alice (via agent)".

Cross-user visible (principle 1): every column that owns a run records the
initiating human, but reads are NOT owner-filtered — within the service every
user with ``agents-access`` sees every run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Job type this producer publishes on /internal/jobs/v1/stream.
JOB_TYPE_AGENT_RUN = "agent_run"

# Run/job lifecycle words. These are the string values of fm_runtime.JobStatus
# (queued|running|paused|completed|failed|canceled); kept as plain strings in
# the column so a newer status never trips a DB enum constraint.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    # A uuid4 hex — also the job_id surfaced to the jobs service. Opaque string
    # so it never collides with search's leads-stream-id job ids.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    goal: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_QUEUED)

    # Attribution (== the job-event fields). owner is the initiating human's
    # preferred_username; origin is always `agent` for a runtime-agent run;
    # actor is the agents service client (azp) that acts downstream.
    owner: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Final natural-language result, or the failure reason.
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Progress bookkeeping: node steps taken and usage snapshot (requests /
    # tool calls / tokens) for observability.
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Coarse fractional progress (0.0-1.0) or NULL when not meaningful.
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
