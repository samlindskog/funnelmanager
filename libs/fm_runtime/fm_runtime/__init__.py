"""Funnel Manager shared service runtime.

Wire-up in each service's main.py, before any other middleware is added:

    from fm_runtime import install
    install(app, service="search", ready_checks={"db": ping_db})

That installs, uniformly across every service:
- PrincipalMiddleware — parses the forwarded JWT into a Principal (the acting
  client rides in `azp`; agent-initiated calls carry `fm_origin=agent`. KC 26.2
  emits no nested `act` chain — the parser keeps it as future-proofing only),
  401s non-@anonymous routes without one,
- structured JSON logging to stdout (principal sub on every line),
- /healthz, /readyz, /metrics,
- /api/<service>/whoami — authenticated principal echo (the hub's discovery probe),
- Prometheus HTTP metrics + structured access log,
- optional OTel/Logfire tracing — a hard no-op unless FM_LOGFIRE=1 (logfire is
  not imported otherwise); requires the `fm-runtime[tracing]` extra to enable.

Outbound internal calls go through InternalClient (token exchange + trace
propagation) — never through a bare httpx client.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from fm_runtime.annotations import anonymous, collect_anonymous
from fm_runtime.client import InternalClient
from fm_runtime.confirmation import (
    AgentApprovalRequired,
    ConfirmationRequired,
    approval_ref,
    confirmation_threshold,
    make_confirm_token,
    make_human_approval,
    require_confirmation,
    verify_human_approval,
)
from fm_runtime.context import RequestContext, current_context, current_principal
from fm_runtime.job_events import (
    JobControlAction,
    JobEvent,
    JobStatus,
    control_path,
)
from fm_runtime.jobs_producer import JobProducer
from fm_runtime.logging import configure_logging
from fm_runtime.middleware import PrincipalMiddleware
from fm_runtime.observability import install_observability
from fm_runtime.principal import (
    ORIGIN_AGENT,
    ORIGIN_USER,
    Actor,
    AuthUnavailableError,
    Peer,
    Principal,
    TokenError,
)
from fm_runtime.settings import RuntimeSettings, get_runtime_settings
from fm_runtime.tokens import ExchangeError, TokenBroker, get_broker, resolve_origin
from fm_runtime.tracing import configure_tracing, tracing_enabled
from fm_runtime.whoami import install_whoami

__all__ = [
    "ORIGIN_AGENT",
    "ORIGIN_USER",
    "Actor",
    "AgentApprovalRequired",
    "AuthUnavailableError",
    "ConfirmationRequired",
    "ExchangeError",
    "InternalClient",
    "JobControlAction",
    "JobEvent",
    "JobProducer",
    "JobStatus",
    "Peer",
    "Principal",
    "RequestContext",
    "RuntimeSettings",
    "TokenBroker",
    "TokenError",
    "anonymous",
    "approval_ref",
    "collect_anonymous",
    "configure_logging",
    "configure_tracing",
    "confirmation_threshold",
    "control_path",
    "current_context",
    "current_principal",
    "get_broker",
    "get_runtime_settings",
    "install",
    "make_confirm_token",
    "make_human_approval",
    "optional_principal",
    "require_confirmation",
    "require_principal",
    "resolve_origin",
    "tracing_enabled",
    "verify_human_approval",
]


def install(app: Any, service: str, ready_checks: dict | None = None) -> None:
    settings = get_runtime_settings()
    configure_logging(service, settings.log_level)
    install_observability(app, ready_checks)
    install_whoami(app, service)
    # Added first so any middleware the service adds afterwards (CORS, ...)
    # runs outside it.
    app.add_middleware(PrincipalMiddleware, service=service, settings=settings)
    # Optional, gated OTel/Logfire tracing. Hard no-op unless FM_LOGFIRE=1
    # (logfire is not even imported otherwise) — zero cost in prod. Wired last so
    # it observes the fully-assembled app; instrumentation attaches out-of-band
    # and does not affect middleware ordering.
    configure_tracing(app, service_name=service, variant=settings.deployment_variant)


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency for handlers that need the acting principal.

    The middleware already rejects unauthenticated calls on non-anonymous
    routes; this exists to hand the Principal to the handler and to stay
    correct even if FM_REQUIRE_PRINCIPAL is ever relaxed."""
    principal = current_principal()
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def optional_principal(request: Request) -> Principal | None:
    """For @anonymous routes that personalize when a principal happens to be
    present but must tolerate its absence (webhooks, callbacks)."""
    return current_principal()
