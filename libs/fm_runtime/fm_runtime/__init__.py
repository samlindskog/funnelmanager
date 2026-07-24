"""Funnel Manager shared service runtime.

Wire-up in each service's main.py, before any other middleware is added:

    from fm_runtime import install
    install(app, service="search", ready_checks={"db": ping_db})

That installs, uniformly across every service:
- PrincipalMiddleware — parses the forwarded JWT into a Principal (sub +
  RFC 8693 `act` chain), 401s non-@anonymous routes without one,
- structured JSON logging to stdout (principal sub on every line),
- /healthz, /readyz, /metrics,
- /api/<service>/whoami — authenticated principal echo (the hub's discovery probe),
- Prometheus HTTP metrics + structured access log.

Outbound internal calls go through InternalClient (token exchange + trace
propagation) — never through a bare httpx client.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from fm_runtime.annotations import anonymous, collect_anonymous
from fm_runtime.client import InternalClient
from fm_runtime.context import RequestContext, current_context, current_principal
from fm_runtime.logging import configure_logging
from fm_runtime.middleware import PrincipalMiddleware
from fm_runtime.observability import install_observability
from fm_runtime.principal import Actor, AuthUnavailableError, Peer, Principal, TokenError
from fm_runtime.settings import RuntimeSettings, get_runtime_settings
from fm_runtime.tokens import ExchangeError, TokenBroker, get_broker
from fm_runtime.whoami import install_whoami

__all__ = [
    "Actor",
    "AuthUnavailableError",
    "ExchangeError",
    "InternalClient",
    "Peer",
    "Principal",
    "RequestContext",
    "RuntimeSettings",
    "TokenBroker",
    "TokenError",
    "anonymous",
    "collect_anonymous",
    "configure_logging",
    "current_context",
    "current_principal",
    "get_broker",
    "get_runtime_settings",
    "install",
    "optional_principal",
    "require_principal",
]


def install(app: Any, service: str, ready_checks: dict | None = None) -> None:
    settings = get_runtime_settings()
    configure_logging(service, settings.log_level)
    install_observability(app, ready_checks)
    install_whoami(app, service)
    # Added first so any middleware the service adds afterwards (CORS, ...)
    # runs outside it.
    app.add_middleware(PrincipalMiddleware, service=service, settings=settings)


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
