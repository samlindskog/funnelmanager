"""Shared job-event + job-control contract for the ``/internal/jobs/v1`` stream.

ONE definition shared by every side, so producers (`search`, `agents`, …) and
the `jobs` consumer never drift:

- Producers expose ``GET /internal/jobs/v1/stream`` — an internal NDJSON stream
  (audience = the producing app; only the `jobs` role may read it) emitting a
  ``JobEvent`` per lifecycle transition.
- The `jobs` service subscribes (exchanging for the app's audience) and upserts
  job state, then exposes control ``POST /internal/jobs/v1/{id}/{action}`` where
  action is a ``JobControlAction``; the owning app maps it onto its own job
  manager. Idempotent; returns the new ``JobStatus``.

Versioning (stability contract): these routes are ``/internal/jobs/v1/*``.
Within v1, changes are **additive only** (new optional fields, new status/type
values are read leniently); a breaking change ships as ``/internal/jobs/v2/*``.
``JobEvent.from_dict`` therefore tolerates unknown keys and preserves them, and
an unrecognized ``status`` from a newer producer is read leniently — the raw
word is preserved (``JobEvent.raw_status`` / ``meta['raw_status']``) and the
event falls back to a safe non-terminal status so it is **never dropped** and a
job is never wedged in a status this version does not understand.

**Strict vs lenient (who gets which):** leniency applies **only** to the
wire-decode paths (``from_dict`` / ``from_json_line``) — the boundary where a
newer peer's additive value legitimately arrives. **Direct construction**
(``JobEvent(...)`` by a first-party producer in *this* codebase) is **strict**:
an unrecognized status is a typo/bug, not additive evolution, so it raises
rather than silently degrading to ``RUNNING``. This makes a producer's status
mistake fail loud in its own process instead of misreporting a job's lifecycle
everywhere downstream.

Dependency-free (stdlib dataclasses + enums), so it imports anywhere a producer
or the consumer runs without pulling a validation stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

JOBS_STREAM_PATH = "/internal/jobs/v1/stream"
JOBS_CONTROL_PATH_TEMPLATE = "/internal/jobs/v1/{job_id}/{action}"


class JobStatus(str, Enum):
    """Lifecycle state of a tracked job. String-valued so it serializes as the
    bare word in NDJSON and compares equal to that word."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @classmethod
    def coerce(cls, value: object) -> JobStatus:
        """Strict parse — raises ``ValueError`` on an unknown status. Use this to
        **validate** a value (e.g. a user-supplied query filter or a control
        reply) where an unrecognized word should be rejected. To read a value off
        the job-event wire without ever dropping it, use :meth:`parse`."""
        try:
            return cls(str(value))
        except ValueError as exc:
            raise ValueError(f"unknown job status {value!r}") from exc

    @classmethod
    def parse(cls, value: object) -> tuple[JobStatus, str | None]:
        """Lenient parse for the event wire — **never raises**.

        Returns ``(status, raw)``: for a known word, ``(that_status, None)``; for
        an unrecognized one, ``(UNKNOWN_STATUS_FALLBACK, <raw string>)`` so the
        caller can preserve the original value while falling back to a safe
        non-terminal status. This is what keeps a newer producer's additive
        status from dropping an event or wedging a job (additive-within-v1)."""
        raw = str(value)
        try:
            return cls(raw), None
        except ValueError:
            return UNKNOWN_STATUS_FALLBACK, raw


#: Statuses a job never leaves — the consumer stops expecting further events.
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
)

#: Safe fallback for an unrecognized wire status. Deliberately **non-terminal**
#: (``RUNNING``) so a status this version does not know can never park a job in a
#: terminal state and stop it from receiving further events; the raw word is kept
#: alongside (see :meth:`JobStatus.parse` / ``JobEvent.raw_status``).
UNKNOWN_STATUS_FALLBACK: JobStatus = JobStatus.RUNNING


class JobControlAction(str, Enum):
    """A write the `jobs` service issues to a producing app's control API."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"

    @classmethod
    def coerce(cls, value: object) -> JobControlAction:
        try:
            return cls(str(value))
        except ValueError as exc:
            raise ValueError(f"unknown job control action {value!r}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobEvent:
    """One lifecycle event on the job stream.

    Fields (the wire schema every app shares):
    - ``job_id``: the producing app's own id for the job (unique within app).
    - ``type``: job kind, e.g. ``apollo_search`` / ``embedding`` / ``agent_run``
      / ``campaign`` (free-form string; the app owns its vocabulary).
    - ``user``: the human owner (``preferred_username``) — records attribution.
    - ``origin``: ``user`` or ``agent`` (the ``fm_origin`` of the initiator).
    - ``actor``: the machine client that acted (``azp``); pairs with ``origin``
      to render "alice (via agent)".
    - ``status``: a :class:`JobStatus`.
    - ``progress``: 0.0–1.0 fractional progress, or ``None`` when not meaningful.
    - ``ts``: ISO-8601 UTC event timestamp.
    - ``exit_status``: terminal detail (e.g. ``ok`` / an error summary); omitted
      until the job reaches a terminal status.
    - ``meta``: app-specific extras (counts, stream ids, cancel handles, …).
    - ``raw_status``: set only when the wire carried a status word this version
      does not recognize — the original string, preserved so nothing is lost
      while ``status`` falls back to a safe non-terminal value. Also mirrored
      into ``meta['raw_status']`` so it survives ``to_dict`` round-trips.
    """

    job_id: str
    type: str
    user: str
    origin: str = "user"
    actor: str = ""
    status: JobStatus = JobStatus.RUNNING
    progress: float | None = None
    ts: str = field(default_factory=_now_iso)
    exit_status: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    raw_status: str | None = None

    def __post_init__(self) -> None:
        # Direct construction is STRICT. A bare status string from a first-party
        # producer is coerced against the known set and a typo RAISES (never a
        # silent degrade to RUNNING). Known words stay exact. The lenient
        # wire-decode paths (from_dict / from_json_line) pre-parse the status
        # into a real JobStatus (preserving raw_status) before calling us, so an
        # unknown status from a newer peer arrives here already a JobStatus and
        # skips this strict branch — leniency lives only at the wire boundary.
        if not isinstance(self.status, JobStatus):
            self.status = JobStatus.coerce(self.status)
        if self.raw_status is not None:
            self.meta.setdefault("raw_status", self.raw_status)
        if self.progress is not None:
            self.progress = float(self.progress)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """Wire form. ``exit_status`` is omitted while ``None`` (additive:
        absent means "not terminal yet")."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "type": self.type,
            "user": self.user,
            "origin": self.origin,
            "actor": self.actor,
            "status": self.status.value,
            "progress": self.progress,
            "ts": self.ts,
            "meta": dict(self.meta),
        }
        if self.exit_status is not None:
            data["exit_status"] = self.exit_status
        return data

    def to_json_line(self) -> str:
        """NDJSON line (trailing newline included) for the stream response."""
        return json.dumps(self.to_dict(), separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobEvent:
        """Parse a wire event — the LENIENT boundary. Unknown keys are preserved
        under ``meta`` so a v1 consumer tolerates additive fields from a newer
        producer, and an unrecognized ``status`` is read leniently: the raw word
        is preserved and ``status`` falls back to a safe non-terminal value so
        the event is **never dropped**. (Direct ``JobEvent(...)`` construction is
        strict — see ``__post_init__``.)"""
        known = {
            "job_id",
            "type",
            "user",
            "origin",
            "actor",
            "status",
            "progress",
            "ts",
            "exit_status",
            "meta",
        }
        meta = dict(data.get("meta") or {})
        for key, value in data.items():
            if key not in known:
                meta[key] = value
        # Pre-parse the status LENIENTLY here (never raises), then hand a real
        # JobStatus to the strict constructor so the unknown word bypasses the
        # strict branch. An unrecognized status is preserved as raw_status and
        # falls back to a safe non-terminal value, never dropped.
        wire_status = data.get("status")
        if wire_status is None:
            status, parsed_raw = JobStatus.RUNNING, None
        else:
            status, parsed_raw = JobStatus.parse(wire_status)
        # raw_status survives a to_dict round-trip via meta['raw_status']: prefer
        # a freshly-parsed unknown word, else recover one carried through meta.
        meta_raw = meta.get("raw_status")
        raw_status = (
            parsed_raw
            if parsed_raw is not None
            else (str(meta_raw) if meta_raw is not None else None)
        )
        event = cls(
            job_id=str(data.get("job_id") or ""),
            type=str(data.get("type") or ""),
            user=str(data.get("user") or ""),
            origin=str(data.get("origin") or "user"),
            actor=str(data.get("actor") or ""),
            status=status,
            progress=(
                None if data.get("progress") is None else float(data["progress"])
            ),
            ts=str(data.get("ts") or _now_iso()),
            exit_status=(
                None if data.get("exit_status") is None else str(data["exit_status"])
            ),
            meta=meta,
            raw_status=raw_status,
        )
        return event

    @classmethod
    def from_json_line(cls, line: str) -> JobEvent:
        return cls.from_dict(json.loads(line))


def control_path(job_id: str, action: JobControlAction | str) -> str:
    """Build a producer's control URL for a job + action."""
    action_value = action.value if isinstance(action, JobControlAction) else str(action)
    return JOBS_CONTROL_PATH_TEMPLATE.format(job_id=job_id, action=action_value)


__all__ = [
    "JOBS_CONTROL_PATH_TEMPLATE",
    "JOBS_STREAM_PATH",
    "TERMINAL_STATUSES",
    "UNKNOWN_STATUS_FALLBACK",
    "JobControlAction",
    "JobEvent",
    "JobStatus",
    "control_path",
]
