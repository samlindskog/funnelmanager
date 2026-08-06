"""ORM models for the agents service — interactive **sessions**.

A ``AgentSession`` is one persistent, multi-turn conversation with a runtime AI
agent (replacing the retired one-shot ``AgentRun``). A **turn** = one user
message → the agent's streamed response (possibly many model requests + tool
calls). Every turn's pydantic-ai ``ModelMessage``s are persisted **verbatim and
append-only** as ``AgentMessage`` rows (the source of truth for replay + display
+ the model context of the next turn).

Attribution (Principle 3): every session records the initiating human
(``owner = preferred_username``), the origin (``origin = fm_origin``, always
``agent`` for runtime-agent work), and the acting client (``actor = azp``) so a
UI renders "alice (via agent)". Attribution is **never** a read filter
(Principle 1): within the service every user with ``agents-access`` sees every
session — the columns attribute a session, they do not hide it. The *one*
sanctioned per-user carve-out is destructive ownership (delete-my-own-session),
enforced in the router as a 404, not here.

A session's **display status is derived** in the list endpoint (running / paused
/ scheduled / error / idle) from live turn state + the last message — it is not a
stored column, so it can never drift from reality.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Job types this producer publishes on /internal/jobs/v1/stream. A turn is one
# short RUNNING→terminal job; a schedule is a persistent SCHEDULED job (a one-shot
# fires RUNNING→COMPLETED, a recurring one stays SCHEDULED). An idle session is
# NOT a job (jobs surfaces running/long-running/scheduled work only — never idle
# state). See app.jobs_registry (producer) + app.scheduler (the poller that fires).
JOB_TYPE_AGENT_TURN = "agent_turn"
JOB_TYPE_AGENT_SCHEDULE = "agent_schedule"

# --- Message kinds (coarse display grouping) --------------------------------
# ``content`` (the serialized ModelMessage) is the real source of truth; ``kind``
# is a coarse label so a UI can group the transcript without re-deriving it from
# every part. Classified from the ModelMessage's parts (see runner._message_kind).
MSG_KIND_USER = "user"
MSG_KIND_ASSISTANT = "assistant"
MSG_KIND_TOOL_CALL = "tool_call"
MSG_KIND_TOOL_RESULT = "tool_result"
MSG_KIND_SUMMARY = "summary"

# --- Turn lifecycle words (string values of fm_runtime.JobStatus) -----------
# Kept as plain strings so a newer status never trips a DB enum constraint.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
STATUS_SCHEDULED = "scheduled"

# Derived session-display statuses (returned by the list endpoint; not stored).
SESSION_STATUS_RUNNING = "running"
SESSION_STATUS_PAUSED = "paused"
SESSION_STATUS_SCHEDULED = "scheduled"
SESSION_STATUS_ERROR = "error"
SESSION_STATUS_IDLE = "idle"

# --- Pending-approval lifecycle (Principle 4 agent hard-enforcement) --------
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
# Terminal without a human decision: the turn ended while the approval still sat
# pending, so it can never be acted on again.
APPROVAL_EXPIRED = "expired"

# --- Schedule lifecycle (row.status; distinct from the schedule's JOB status) --
# A schedule row is `scheduled` while pending, `completed` once a one-shot has
# fired (or a recurring cron has no further run), or `canceled` when cancelled via
# the jobs control API. The DB-polling scheduler only ever selects `scheduled`.
SCHEDULE_SCHEDULED = "scheduled"
SCHEDULE_CANCELED = "canceled"
SCHEDULE_COMPLETED = "completed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentSession(Base):
    """One persistent, multi-turn conversation with a runtime AI agent."""

    __tablename__ = "agent_session"

    # A uuid4 hex. Opaque so it never collides with search's stream-id job ids.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Attribution (Principle 3) — never a read filter (Principle 1). owner is the
    # initiating human's preferred_username; origin is `agent` for runtime-agent
    # work; actor is the agents service client (azp) acting downstream.
    owner: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Auto-derived from the first user message, renameable by the owner.
    title: Mapped[str] = mapped_column(String(400), nullable=False, default="New session")

    # The OpenAI chat model selected for this session's turns (bare id, no
    # provider prefix). Turns build OpenAIChatModel(model, provider=…) from it.
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # ``seq`` up to which the verbatim history has been summarized: the context
    # fed to the next turn is (summary rows) + (messages with seq > watermark).
    context_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ``error`` remembers a failed last turn so the derived status can show it
    # even after the in-memory turn buffer has expired.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AgentMessage(Base):
    """One verbatim, append-only pydantic-ai ``ModelMessage`` in a session.

    ``content`` is the ModelMessage serialized via ``ModelMessagesTypeAdapter`` —
    the source of truth for transcript replay/display AND for reconstructing the
    model context of the next turn (deserialized straight back). ``kind`` is a
    coarse display label derived from the message's parts. ``usage`` (a serialized
    ``RunUsage``) is stamped on the assistant row that ended a turn.
    """

    __tablename__ = "agent_message"
    __table_args__ = (
        # Append-only ordering within a session; no two rows share a seq.
        UniqueConstraint("session_id", "seq", name="uq_agent_message_session_seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Monotonic per session — the transcript order. Assigned server-side.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # The turn (one user message → response) this message belongs to. Groups a
    # multi-message assistant turn and keys the Principle-4 approval to a turn.
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # The serialized ModelMessage (ModelRequest | ModelResponse) — source of truth.
    content: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # The model that produced an assistant/tool_call row (NULL for user rows).
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Per-turn RunUsage snapshot, on the final assistant row of a turn (else NULL).
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AgentSchedule(Base):
    """A persisted schedule for a session — future/recurring runtime-agent work.

    Written by the internal ``schedule_agent_job`` tool (``app.scheduler``) a
    running turn calls, and fired by the in-process poller (``app.scheduler``):
    when ``next_run_at`` arrives the poller spawns a fresh background turn on the
    detached execution path. A one-shot (``spec = {"at": iso}``) completes after
    its single firing; a recurring one (``spec = {"cron": expr}``) recomputes
    ``next_run_at`` and stays ``scheduled``. Surfaces on ``/internal/jobs`` as an
    ``agent_schedule`` job (id ``sched-<id>``) and drives the derived "scheduled"
    session status. Attribution (Principle 3) mirrors the session's; never a read
    filter (Principle 1). Cascade-deletes with its session.
    """

    __tablename__ = "agent_schedule"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    owner: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # One-shot ``{"at": iso}`` or recurring ``{"cron": expr}``.
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SCHEDULE_SCHEDULED, index=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class PendingApproval(Base):
    """A Principle-4 pending human approval for an over-threshold agent action.

    When an MCP tool call returns the fm_runtime agent-approval gate
    (``needs_human_approval`` — e.g. a large campaign or a big embeddings
    backfill), the running **turn** pauses and records one of these. A runtime
    agent can never self-confirm; the ONLY way the action proceeds is the
    initiating human approving it here, after which the ``agents`` service mints
    an unforgeable ``human_approval`` token (bound to this exact
    ``action`` + ``estimate`` + ``subject``) and resumes the turn so it re-issues
    the tool call WITH the approval (token injected server-side, never by the LLM).

    Re-keyed from the retired run model to ``(session_id, turn_id)``: the paused
    unit is now a turn within a session. Cross-user visible for *reading*
    (Principle 1); only the initiating human (``subject``/session ``owner``) may
    *decide* it — enforced in the approvals router. The minted token is
    deliberately **not** persisted — it rides only the in-memory coordinator.
    """

    __tablename__ = "agent_pending_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The session + turn this approval gates. Cascade so a deleted session drops
    # its approvals.
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # The human whose action this is — the session owner. The minted token binds
    # to this subject so it can never be replayed for a different human.
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # fm_runtime's stable approval reference for (action, estimate-bucket, subject).
    approval_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

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
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ConsumedApproval(Base):
    """A **spent** Principle-4 approval reference — the single-use ledger.

    The fm_runtime ``human_approval`` token this service mints is *replayable
    within its TTL* for the same (action, estimate-bucket, subject); nothing in
    the token itself makes it one-shot. This table is the consumed-ref hook that
    closes that: once a paused turn resumes with an approval and the re-issued MCP
    action is accepted, its ``approval_ref`` is recorded here. Thereafter the
    approvals API refuses to mint a second token for that ref and the resume path
    refuses to re-issue one — so one human approval authorizes **exactly one**
    over-threshold action, never a replayed second.

    Keyed by ``approval_ref`` (the PK) so recording a spend is an atomic
    check-and-set even under concurrent approvals on the same bucket. Deliberately
    **no** FK to ``agent_session`` — a consumed ref must outlive any session
    cleanup, or deleting the session would re-open the token to replay.
    """

    __tablename__ = "agent_consumed_approvals"

    approval_ref: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Provenance of the spend (audit / debugging).
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approval_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
