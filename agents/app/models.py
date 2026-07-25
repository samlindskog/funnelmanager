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

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Job type this producer publishes on /internal/jobs/v1/stream.
JOB_TYPE_AGENT_RUN = "agent_run"

# Pending-approval lifecycle (Principle 4 agent hard-enforcement). An
# over-threshold action a runtime agent attempted, awaiting its initiating
# human's out-of-band decision. A runtime agent can NEVER self-confirm, so an
# approval only ever leaves `pending` via the owner-gated approvals API.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
# Terminal without a human decision: the run was canceled/ended while the
# approval still sat pending, so it can never be acted on again.
APPROVAL_EXPIRED = "expired"

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


class PendingApproval(Base):
    """A Principle-4 pending human approval for an over-threshold agent action.

    When an MCP tool call returns the fm_runtime agent-approval gate
    (``needs_human_approval`` — e.g. launching a large campaign or a big
    embeddings backfill), the runtime-agent run **pauses** and records one of
    these. A runtime agent can never self-confirm; the ONLY way the action
    proceeds is the initiating human approving here, after which the ``agents``
    service mints an unforgeable ``human_approval`` token (bound to this exact
    ``action`` + ``estimate`` + ``subject``) and resumes the run so it re-issues
    the tool call WITH the approval.

    ``tool_name``/``tool_args`` capture the exact gated MCP call so the resumed
    run re-issues *that* action (token injected server-side, never by the LLM).
    The minted token is deliberately **not** persisted — it lives only in memory
    on the live run's coordinator, so a DB row can never leak a bearer-equivalent.

    Cross-user visible for *reading* (principle 1), but only the initiating human
    (``AgentRun.owner``) may *decide* it — enforced in the approvals router.
    """

    __tablename__ = "agent_pending_approvals"

    # uuid4 hex — the `aid` in POST /api/agents/tasks/{id}/approvals/{aid}.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The run this approval gates. Cascade so a deleted run drops its approvals.
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The human whose action this is — always the run's owner (initiating human).
    # The minted approval token binds to this subject so it can never be replayed
    # for a different human.
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # fm_runtime's stable approval reference for (action, estimate-bucket, subject)
    # — dedup key + audit correlation with the gate that raised it.
    approval_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # The gated action identity (opaque string from the gate, e.g.
    # "campaign:42:start") and the resource estimate that tripped the threshold.
    action: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # The exact MCP tool call to re-issue on approval (token injected server-side).
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool_args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=APPROVAL_PENDING, index=True
    )
    # Who decided it (must equal subject) and when.
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ConsumedApproval(Base):
    """A **spent** Principle-4 approval reference — the single-use ledger.

    The fm_runtime ``human_approval`` token this service mints is *replayable
    within its TTL* for the same (action, estimate-bucket, subject): the token
    binds to those, and ``approval_ref`` is their stable hash. Nothing in the
    token itself makes it one-shot. This table is the consumed-ref hook that
    closes that: once a paused run resumes with an approval and the re-issued MCP
    action is accepted (the gate let it through), its ``approval_ref`` is recorded
    here. Thereafter the approvals API refuses to mint a second token for that ref
    and the resume path refuses to re-issue one — so one human approval authorizes
    **exactly one** over-threshold action, never a replayed second.

    Keyed by ``approval_ref`` (the primary key), so recording a spend is an atomic
    check-and-set even under concurrent approvals landing on the same bucket: the
    first insert wins, a duplicate raises and is caught. Deliberately **no** FK to
    ``agent_runs`` — a consumed ref must outlive any run-row cleanup, or deleting
    the run would re-open the token to replay for its remaining TTL.
    """

    __tablename__ = "agent_consumed_approvals"

    # The stable fm_runtime approval_ref (action|estimate-bucket|subject). Unique
    # by being the PK — this is what makes the spend atomic and idempotent.
    approval_ref: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Provenance of the spend (audit / debugging) — which run and which pending
    # approval consumed the ref, and the human + action it authorized.
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    approval_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
