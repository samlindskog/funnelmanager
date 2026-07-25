from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import Job


class JobOut(BaseModel):
    """A tracked job as surfaced to MCP clients. Cross-user visible: every
    caller with the ``jobs`` audience sees every job (principle 1)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    app: str
    external_job_id: str
    type: str
    # Attribution (never used to hide): human owner + how it was initiated.
    user: str
    origin: str
    actor: str
    status: str
    progress: float | None
    exit_status: str | None
    started_at: datetime | None
    ended_at: datetime | None
    last_event_at: datetime | None
    meta: dict

    @classmethod
    def of(cls, job: Job) -> "JobOut":
        return cls.model_validate(job)


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int
    limit: int
    offset: int


class ControlResultOut(BaseModel):
    """Result of a pause/resume/cancel proxied to the owning app. Idempotent —
    re-issuing an action that already took effect returns the same status."""

    id: int
    app: str
    external_job_id: str
    action: str
    status: str
