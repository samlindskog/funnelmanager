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

Human vs. agent origin — the hard enforcement (Principle 4, decided Phase 4)
----------------------------------------------------------------------------
The ``confirm=true`` re-invoke above is a **human** control: it presumes a person
saw the estimate and opted in. A **runtime AI agent can never self-confirm** an
over-threshold action — otherwise the LLM could bypass the whole gate by simply
resending with ``confirm=true``. So ``require_confirmation`` takes an ``origin``
(``"user"`` default, or ``"agent"`` — from ``Principal.origin`` / the ``fm_origin``
claim), and when the action is over threshold **and** ``origin == "agent"``:

- ``confirm=true`` is **ignored** (it cannot lift the gate), and
- the call raises ``AgentApprovalRequired`` — a *distinct* exception (NOT a
  ``ConfirmationRequired``, so ordinary "re-invoke with confirm=true" handlers do
  not catch it) carrying ``needs_human_approval=true``, the estimate, the action,
  and a stable ``approval_ref``. The ``agents`` service catches this, pauses the
  run, and surfaces a pending approval to the initiating human in ``agentsui``.

The **only** way an over-threshold agent action proceeds is an **unforgeable
``human_approval`` token**, minted by the ``agents`` service *after* the human
approves out-of-band, bound to the exact ``action`` + ``estimate`` (and
optionally the human's ``subject``). ``require_confirmation`` verifies it with an
HMAC over a platform secret (``FM_CONFIRM_APPROVAL_SECRET``) that the runtime
agent (the LLM) does not hold — so the agent cannot forge it. Verification is
**fail-closed**: no secret configured, or a bad/expired/mismatched token ⇒ the
approval is rejected and ``AgentApprovalRequired`` is raised.

Usage (a service guarding an expensive action)::

    from fm_runtime import require_confirmation, confirmation_threshold

    GB = 1024 ** 3
    threshold = confirmation_threshold("MAIL_BACKUP_CONFIRM_BYTES", 2 * GB)

    @router.post("/api/mail/mailboxes/{id}/backup")
    async def backup(id: str, confirm: bool = False, human_approval: str | None = None,
                     principal: Principal = Depends(require_principal)):
        estimate = await estimate_backup_bytes(id)
        require_confirmation(
            estimate, threshold,
            confirm=confirm, unit="bytes", action=f"backup:{id}",
            human_approval=human_approval,  # forwarded from the caller, if any
            # origin + subject default from the current principal — safe-by-
            # default, so forgetting them still hard-enforces for an agent.
            message="This mailbox backup is large; confirm to proceed.",
        )
        ...  # actually run it

Usage (the ``agents`` service minting an approval once the human clicks approve)::

    from fm_runtime import make_human_approval
    token = make_human_approval(action="backup:42", estimate=5 * GB, subject="alice")
    # hand `token` back to the paused run; it re-invokes with human_approval=token

The ``confirm_token`` (the human flow) binds the confirmation to the estimate the
user was shown, so a client cannot be tricked into confirming a wildly different
amount. It is advisory: the load-bearing human gate is ``confirm=True``. Pass
``verify_token=True`` to additionally require the echoed token to match. The
``human_approval`` token, by contrast, **is** a security boundary: it is the sole
lever that lets an over-threshold agent action run.

Binding is **tight and relative** (~1%): the token is bound to the estimate at a
~1% resolution at every magnitude (``_estimate_bucket``), so an approval minted
for a small N (a few GB, a handful of recipients) cannot authorize a materially
larger action. It is also **subject fail-closed** (a token minted for alice
cannot be used without naming alice) and supports **single-use**: pass a
``consumed`` set/predicate keyed by ``approval_ref`` and mark the ref consumed
after use so a token cannot be replayed within its TTL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
from typing import Any, Callable, Container, Union

from fastapi import HTTPException

from fm_runtime.context import current_principal
from fm_runtime.principal import ORIGIN_AGENT, ORIGIN_USER

CONFIRMATION_REQUIRED = "confirmation_required"
HUMAN_APPROVAL_REQUIRED = "human_approval_required"

# Relative tolerance for binding a token to an estimate. The estimate is
# recomputed on the confirm/approval call and may jitter slightly from the
# value first shown; the token must survive that jitter WITHOUT letting an
# approval for N authorize a materially larger action. So the binding is
# *relative*: buckets are ~1% wide at EVERY magnitude (see _estimate_bucket),
# not a fixed absolute window. At small magnitudes (a handful of GB, a few
# recipients) that is the difference between "approve 3 → run up to ~49" (a
# fixed 100-unit window) and "approve 3 → run ~3" (this).
_TOKEN_BUCKET_RATIO = 0.01

# How many ~1% buckets of slack the binding tolerates between the estimate first
# shown and the value recomputed on the confirm/approval call. 1 ⇒ a ~1-2%
# relative window — wide enough to survive a re-sampled estimate's jitter (e.g.
# the mail backup byte estimate), tight enough that an approval for N can never
# authorize a materially larger action (3 and 49 are ~130 buckets apart).
_BUCKET_TOLERANCE = 1

# A consumed-ref hook: either a set/dict/Container tested with ``in`` or a
# predicate ``ref -> bool`` returning True when the ref was already spent.
ConsumedRef = Union[Container[str], Callable[[str], bool]]

# Env var (read via FM_CONFIRM_<NAME> or bare <NAME>) holding the platform HMAC
# secret used to sign/verify human_approval tokens. Configured on the `agents`
# service (which mints) and on every service that guards agent-initiated
# expensive actions (which verifies). The runtime agent / LLM never sees it.
_APPROVAL_SECRET_ENV = "APPROVAL_SECRET"

# Default lifetime of a minted human_approval token. A human approval is a
# point-in-time decision; it should not sit valid indefinitely.
_APPROVAL_TTL_SECONDS_DEFAULT = 3600

_APPROVAL_PREFIX = "fmha1"  # fm human-approval, format v1


class ConfirmationRequired(HTTPException):
    """HTTP 409 telling the caller an expensive action needs explicit
    confirmation. The structured ``detail`` is what the UI / agent reads.

    This is the **human** gate: the caller re-invokes with ``confirm=true``.
    Over-threshold *agent* actions raise ``AgentApprovalRequired`` instead."""

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


class AgentApprovalRequired(HTTPException):
    """HTTP 409 telling the caller that an **agent-initiated** over-threshold
    action cannot self-confirm and needs a human's out-of-band approval.

    Deliberately **not** a subclass of ``ConfirmationRequired`` — a handler that
    catches ``ConfirmationRequired`` (and answers "re-invoke with confirm=true")
    must NOT swallow this, because ``confirm=true`` is exactly what the runtime
    agent must not be able to use to bypass the gate. The ``agents`` service
    catches this, pauses the run, records a pending approval keyed by
    ``approval_ref``, and surfaces it to the initiating human. Once the human
    approves, the service mints a ``human_approval`` token (``make_human_approval``)
    and resumes the run, which re-invokes with ``human_approval=<token>``."""

    def __init__(
        self,
        *,
        estimate: float,
        threshold: float,
        approval_ref: str,
        unit: str = "",
        action: str = "",
        subject: str = "",
        message: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "error": HUMAN_APPROVAL_REQUIRED,
            "needs_human_approval": True,
            "estimate": estimate,
            "threshold": threshold,
            "unit": unit,
            "action": action,
            "subject": subject,
            "approval_ref": approval_ref,
            "message": message
            or (
                f"An AI agent requested an action estimated at "
                f"{estimate}{(' ' + unit) if unit else ''}, over the "
                f"{threshold}{(' ' + unit) if unit else ''} threshold. An agent "
                "cannot self-confirm; a human must approve this before it runs."
            ),
        }
        if meta:
            detail["meta"] = meta
        super().__init__(status_code=409, detail=detail)


def _estimate_bucket(estimate: float) -> int:
    """Quantize an estimate to a TIGHT, *relative* bucket so a token survives
    tiny recompute jitter yet cannot authorize a materially larger action.

    Each integer bucket is a ~``_TOKEN_BUCKET_RATIO`` (1%) multiplicative step
    (a log scale), so the bucket is ~1% wide at every magnitude — 3 and 49 land
    in wildly different buckets (unlike a fixed 100-unit window, where both
    rounded to 0 and an approval for 3 silently authorized ~49). ``round``
    keeps a bucket's span to a single ~1% step, so an approval for estimate E
    binds actions to within ~±0.5% of E."""
    e = abs(float(estimate))
    if e <= 0.0:
        return 0
    return int(round(math.log(e) / math.log(1.0 + _TOKEN_BUCKET_RATIO)))


def _confirm_token(action: str, estimate: float) -> str:
    """Deterministic, opaque token binding a confirmation to (action, estimate).

    Not a security boundary (the real human gate is confirm=true); it only stops
    a stale/mismatched confirm from sliding through. Bucketed so it survives the
    tiny recompute jitter between the estimate and the confirm request."""
    payload = f"{action}|{_estimate_bucket(estimate)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def make_confirm_token(action: str, estimate: float) -> str:
    """Public helper: the token a client should echo back with confirm=true."""
    return _confirm_token(action, estimate)


def approval_ref(action: str, estimate: float, subject: str = "") -> str:
    """Stable, deterministic reference for the pending human approval of an
    over-threshold agent action. Same (action, estimate-bucket, subject) ⇒ same
    ref, so the ``agents`` service can dedupe pending approvals and a retry of
    the paused run maps back to the same approval request. Not a secret."""
    payload = f"{action}|{_estimate_bucket(estimate)}|{subject}"
    return "aref_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


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


def _approval_secret(explicit: str | None) -> str:
    """The HMAC secret for human_approval tokens: an explicit override, else
    ``FM_CONFIRM_APPROVAL_SECRET`` / ``APPROVAL_SECRET``. Empty string when
    unconfigured — callers treat that as fail-closed (cannot mint / cannot
    verify), never as "skip the check"."""
    if explicit is not None and explicit.strip():
        return explicit
    for name in (f"FM_CONFIRM_{_APPROVAL_SECRET_ENV}", _APPROVAL_SECRET_ENV):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw
    return ""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(secret: str, payload_b64: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256)
    return _b64u(mac.digest())


def make_human_approval(
    action: str,
    estimate: float,
    *,
    subject: str = "",
    secret: str | None = None,
    ttl_seconds: int = _APPROVAL_TTL_SECONDS_DEFAULT,
) -> str:
    """Mint the unforgeable token that lets an over-threshold **agent** action
    proceed. Called by the ``agents`` service *after* a human approves the
    pending action in ``agentsui`` — never by the runtime agent itself.

    The token is an HMAC (over ``FM_CONFIRM_APPROVAL_SECRET``) binding the
    approval to ``action`` + the bucketed ``estimate`` (and, if given, the
    approving human's ``subject``) with an expiry. ``require_confirmation``
    accepts it for an over-threshold ``origin="agent"`` action. Because the
    runtime agent/LLM never holds the secret, it cannot forge one.

    Raises ``RuntimeError`` if no secret is configured — an approval token with
    no key would be forgeable, so we refuse to mint a fake-secure token."""
    secret = _approval_secret(secret)
    if not secret:
        raise RuntimeError(
            "Cannot mint a human_approval token: no approval secret configured "
            f"(set FM_CONFIRM_{_APPROVAL_SECRET_ENV}). Refusing to issue an "
            "unsigned, forgeable approval."
        )
    payload = {
        "action": action,
        "bucket": _estimate_bucket(estimate),
        "sub": subject,
        "ref": approval_ref(action, estimate, subject),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{_APPROVAL_PREFIX}.{payload_b64}.{_sign(secret, payload_b64)}"


def _ref_consumed(consumed: ConsumedRef | None, ref: str) -> bool:
    """Consult the optional single-use hook. Returns True (⇒ reject) when the
    ref has already been spent. **Fail-closed**: if the hook raises, treat the
    ref as consumed rather than let a replay through."""
    if consumed is None or not ref:
        return False
    try:
        if callable(consumed):
            return bool(consumed(ref))
        return ref in consumed
    except Exception:  # noqa: BLE001 — a broken store must not open the gate
        return True


def verify_human_approval(
    token: str | None,
    action: str,
    estimate: float,
    *,
    subject: str | None = None,
    secret: str | None = None,
    consumed: ConsumedRef | None = None,
) -> bool:
    """Return True only for a genuine, unexpired, unspent ``human_approval``
    token bound to this exact ``action`` + relative-bucketed ``estimate`` and
    (when the token carries one) the approving human's ``subject``.

    **Fail-closed** — every one of these returns False: a missing token, an
    unconfigured secret, a bad signature, an expired token, any binding
    mismatch, a token minted for a subject when the caller supplied none (see
    below), or a ref the ``consumed`` hook reports as already spent.

    Subject binding is fail-closed: if the token was minted *for a specific
    human* (non-empty ``sub``), the caller MUST supply the matching ``subject``
    — omitting it (``subject=None``) is rejected, so an approval minted for
    alice can never authorize bob's (or an un-attributed) agent action. If the
    token carries no ``sub`` (a deliberately un-scoped approval) it still may
    not be used where a specific ``subject`` is demanded.

    Single-use: pass ``consumed`` (a set/dict, or a ``ref -> bool`` predicate)
    keyed by the token's stable ``approval_ref``; a ref already in it is
    rejected. The service marks the ref consumed itself after a successful use
    (this function only *consults*, never mutates)."""
    if not token:
        return False
    secret = _approval_secret(secret)
    if not secret:
        return False  # no key ⇒ nothing is trustworthy ⇒ reject
    try:
        prefix, payload_b64, sig = token.split(".", 2)
    except ValueError:
        return False
    if prefix != _APPROVAL_PREFIX:
        return False
    if not hmac.compare_digest(sig, _sign(secret, payload_b64)):
        return False
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("action") != action:
        return False
    token_bucket = payload.get("bucket")
    if not isinstance(token_bucket, int) or isinstance(token_bucket, bool):
        return False
    if abs(token_bucket - _estimate_bucket(estimate)) > _BUCKET_TOLERANCE:
        return False
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return False
    token_sub = str(payload.get("sub") or "")
    if token_sub:
        # Token is bound to a specific human: the caller must name that same
        # human. Omitting the subject (None) is a fail-closed reject.
        if subject is None or subject != token_sub:
            return False
    elif subject:
        # Token carries no subject binding, but the caller demands one.
        return False
    if _ref_consumed(consumed, str(payload.get("ref") or "")):
        return False
    return True


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
    origin: str | None = None,
    human_approval: str | None = None,
    subject: str | None = None,
    approval_secret: str | None = None,
    consumed: ConsumedRef | None = None,
) -> float:
    """Gate an expensive action. Returns the estimate when it is safe to
    proceed; raises otherwise.

    Under threshold ⇒ always proceeds (cheap enough), regardless of origin.

    Over threshold, the behavior depends on ``origin``:

    - ``origin != "agent"`` (a human) — unchanged legacy behavior: proceeds when
      ``confirm`` is True (with ``verify_token=True`` the echoed ``confirm_token``
      must also match), else raises ``ConfirmationRequired`` (409).

    - ``origin == "agent"`` — **hard enforcement**: ``confirm=true`` is IGNORED (a
      runtime agent can never self-confirm). The action proceeds ONLY if
      ``human_approval`` is a valid, unexpired token (``make_human_approval``)
      bound to this ``action``+``estimate`` (and ``subject`` when the token binds
      one). Otherwise raises ``AgentApprovalRequired`` (409,
      ``needs_human_approval=true``) with a stable ``approval_ref`` for the
      ``agents`` service to pause on.

    **Safe-by-default origin.** ``origin`` defaults to the *current principal's*
    origin (``current_principal().origin``), NOT to ``"user"``. So a guard that
    forgets to pass ``origin=`` still hard-enforces for an agent-initiated
    request instead of silently dropping into the human ``confirm=true`` branch.
    Pass ``origin=`` explicitly to override (e.g. a detached job). With no
    principal in context, it falls back to ``"user"`` (legacy).

    **Safe-by-default subject.** ``subject`` likewise defaults to the current
    principal's ``username``, so the approval is bound to (and checked against)
    the acting human even when a guard omits ``subject=``. Pass ``subject=""``
    to opt out explicitly.

    ``consumed`` (a set/dict or ``ref -> bool`` predicate keyed by
    ``approval_ref``) enforces single-use of the ``human_approval`` token; see
    ``verify_human_approval``."""
    # Safe-by-default: derive origin/subject from the acting principal when the
    # caller did not pass them, so a forgotten kwarg fails safe (hard-enforces
    # for agents) rather than opening the human confirm=true branch.
    principal = current_principal() if (origin is None or subject is None) else None
    if origin is None:
        origin = principal.origin if principal is not None else ORIGIN_USER
    if subject is None:
        subject = principal.username if principal is not None else None

    if estimate <= threshold:
        return estimate

    if origin == ORIGIN_AGENT:
        # Hard enforcement: confirm=true cannot lift the gate for an agent. The
        # only lever is an unforgeable, human-minted approval token.
        if verify_human_approval(
            human_approval,
            action,
            estimate,
            subject=subject,
            secret=approval_secret,
            consumed=consumed,
        ):
            return estimate
        raise AgentApprovalRequired(
            estimate=estimate,
            threshold=threshold,
            approval_ref=approval_ref(action, estimate, subject or ""),
            unit=unit,
            action=action,
            subject=subject or "",
            message=message,
            meta=meta,
        )

    # Human origin — the existing estimate→confirm flow, unchanged.
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
    "HUMAN_APPROVAL_REQUIRED",
    "AgentApprovalRequired",
    "ConfirmationRequired",
    "approval_ref",
    "confirmation_threshold",
    "make_confirm_token",
    "make_human_approval",
    "require_confirmation",
    "verify_human_approval",
]
