"""Principle 4 — guard expensive actions with an estimate/confirm gate.

Any operation that may consume an extreme amount of resources (a full mailbox
backup, a large embeddings backfill, a huge search or campaign) must **estimate
first and require confirmation before proceeding** — never silently run
something costly. This module makes every service do it identically.

The contract (stable across services):

- The endpoint computes an estimate (a number in whatever unit it cares about).
- ``require_confirmation`` compares it to a **configurable threshold**:
  - estimate ``<=`` threshold  → returns (proceed; it is cheap enough),
  - ``confirm=True``           → returns (the caller explicitly opted in),
  - otherwise                  → raises ``ConfirmationRequired`` (HTTP 409),
    whose body carries the estimate, the threshold, the unit, a human message,
    and an opaque ``confirm_token``.
- The caller re-invokes the same endpoint with ``confirm=true`` (a UI renders a
  dialog; a runtime AI agent MUST escalate to its human — never auto-confirm).

Usage::

    from fm_runtime import require_confirmation, confirmation_threshold

    GB = 1024 ** 3
    threshold = confirmation_threshold("MAIL_BACKUP_CONFIRM_BYTES", 2 * GB)

    @router.post("/api/mail/mailboxes/{id}/backup")
    async def backup(id: str, confirm: bool = False):
        estimate = await estimate_backup_bytes(id)
        require_confirmation(
            estimate, threshold,
            confirm=confirm, unit="bytes", action=f"backup:{id}",
            message="This mailbox backup is large; confirm to proceed.",
        )
        ...  # actually run it

The confirm token binds the confirmation to the estimate the user was shown, so
a client cannot be tricked into confirming a wildly different amount. It is
advisory: the load-bearing gate is ``confirm=True``. Pass ``verify_token=True``
to additionally require the echoed token to match.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from fastapi import HTTPException

CONFIRMATION_REQUIRED = "confirmation_required"

# Round the estimate into buckets before it goes into the confirm token, so the
# token stays stable across tiny estimate jitter between the estimate call and
# the confirm call (which recomputes it) — 1% granularity.
_TOKEN_BUCKET_RATIO = 0.01


class ConfirmationRequired(HTTPException):
    """HTTP 409 telling the caller an expensive action needs explicit
    confirmation. The structured ``detail`` is what the UI / agent reads."""

    def __init__(
        self,
        *,
        estimate: float,
        threshold: float,
        confirm_token: str,
        unit: str = "",
        action: str = "",
        message: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "error": CONFIRMATION_REQUIRED,
            "estimate": estimate,
            "threshold": threshold,
            "unit": unit,
            "action": action,
            "confirm_token": confirm_token,
            "message": message
            or (
                f"Estimated {estimate}{(' ' + unit) if unit else ''} exceeds the "
                f"{threshold}{(' ' + unit) if unit else ''} threshold; "
                "re-invoke with confirm=true to proceed."
            ),
        }
        if meta:
            detail["meta"] = meta
        super().__init__(status_code=409, detail=detail)


def _confirm_token(action: str, estimate: float) -> str:
    """Deterministic, opaque token binding a confirmation to (action, estimate).

    Not a security boundary (the real gate is confirm=true); it only stops a
    stale/mismatched confirm from sliding through. Bucketed so it survives the
    tiny recompute jitter between the estimate and the confirm request."""
    bucket = round(abs(estimate) * _TOKEN_BUCKET_RATIO)
    payload = f"{action}|{bucket}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def make_confirm_token(action: str, estimate: float) -> str:
    """Public helper: the token a client should echo back with confirm=true."""
    return _confirm_token(action, estimate)


def confirmation_threshold(env_var: str, default: float) -> float:
    """Read a configurable threshold from ``FM_CONFIRM_<ENV_VAR>`` (falling back
    to a bare ``<ENV_VAR>``), else the default. Bad values fall back too, so a
    typo never disables the guard silently as a crash."""
    for name in (f"FM_CONFIRM_{env_var}", env_var):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            try:
                return float(raw)
            except ValueError:
                continue
    return float(default)


def require_confirmation(
    estimate: float,
    threshold: float,
    *,
    confirm: bool = False,
    confirm_token: str | None = None,
    unit: str = "",
    action: str = "",
    message: str | None = None,
    meta: dict[str, Any] | None = None,
    verify_token: bool = False,
) -> float:
    """Gate an expensive action. Returns the estimate when it is safe to
    proceed; raises ``ConfirmationRequired`` (409) otherwise.

    Proceeds when the estimate is at/under ``threshold`` (cheap enough) or when
    ``confirm`` is True. With ``verify_token=True`` a confirm must additionally
    echo the token from the 409 body (bound to action+estimate)."""
    if estimate <= threshold:
        return estimate
    if confirm:
        if verify_token and confirm_token != make_confirm_token(action, estimate):
            raise ConfirmationRequired(
                estimate=estimate,
                threshold=threshold,
                confirm_token=make_confirm_token(action, estimate),
                unit=unit,
                action=action,
                message="confirm_token does not match the current estimate; "
                "re-fetch the estimate and confirm again.",
                meta=meta,
            )
        return estimate
    raise ConfirmationRequired(
        estimate=estimate,
        threshold=threshold,
        confirm_token=make_confirm_token(action, estimate),
        unit=unit,
        action=action,
        message=message,
        meta=meta,
    )


__all__ = [
    "CONFIRMATION_REQUIRED",
    "ConfirmationRequired",
    "confirmation_threshold",
    "make_confirm_token",
    "require_confirmation",
]
