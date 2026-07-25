from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Job(Base):
    """One tracked unit of async work, mirrored from a producing app's
    ``/internal/jobs/v1/stream``.

    Cross-user visible BY DESIGN (guiding principle 1): every field attributes
    the work (``user``/``origin``/``actor``) but NOTHING here is ever used to
    filter a read by owner — Keycloak's audience+role is the only access gate.
    Reads attribute, they do not hide.

    Identity: ``id`` is this service's own surrogate key; a job is uniquely
    keyed by ``(app, external_job_id)`` — the producing app's own job id, which
    is only unique within that app.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("app", "external_job_id", name="uq_jobs_app_external_id"),
        # Cross-user list filters (principle 1: filter by attribute, never hide).
        Index("ix_jobs_user", "job_user"),
        Index("ix_jobs_app_status", "app", "status"),
        Index("ix_jobs_last_event_at", "last_event_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Producing app (also the token-exchange audience for control/stream) and
    # that app's own id for the job.
    app: Mapped[str] = mapped_column(String(64), nullable=False)
    external_job_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Free-form job kind owned by the producing app (apollo_search / embedding /
    # agent_run / campaign / ...).
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # Attribution (never a filter). ``user`` = human owner (preferred_username);
    # column is named "job_user" because ``user`` is a reserved word in
    # Postgres. ``origin`` = user|agent (fm_origin); ``actor`` = the machine
    # client that acted (azp) — together they render "alice (via agent)".
    user: Mapped[str] = mapped_column("job_user", String(128), nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # Lifecycle. ``status`` holds a fm_runtime.JobStatus value (queued|running|
    # paused|completed|failed|canceled).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_status: Mapped[str | None] = mapped_column(String(512), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Timestamp of the most recent lifecycle event applied — the ordering guard
    # for idempotent, replay-safe upserts (see store.apply_event).
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # App-specific extras (counts, stream ids, cancel handles, unknown v>1 keys).
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
