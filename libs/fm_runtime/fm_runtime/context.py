"""Request-scoped context: principal, peer workload, trace headers.

Set by PrincipalMiddleware at ingress; read anywhere (handlers, log
formatter, outbound InternalClient) without threading arguments through every
call. A task spawned via asyncio.create_task copies the current context, so a
background job started mid-request inherits these values; InternalClient.detached
still explicitly freezes what it needs so work that outlives the request (and
its contextvar reset) keeps a stable subject token / trace context."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from fm_runtime.principal import Peer, Principal

# W3C + zipkin headers to forward verbatim on outbound internal calls.
TRACE_HEADERS = (
    "traceparent",
    "tracestate",
    "x-request-id",
    "x-b3-traceid",
    "x-b3-spanid",
    "x-b3-parentspanid",
    "x-b3-sampled",
    "x-b3-flags",
    # Canary routing marker, forwarded verbatim internally so canary requests
    # route to <svc>-canary workloads where they exist and fall back to stable
    # otherwise. NOT trusted for authz. At the gateway the canary-cookie-gate
    # EnvoyFilter (deploy/infrastructure/mesh-policies/canary-cookie-gate.yaml)
    # now strips any client-supplied x-fm-canary and re-injects it only from the
    # validated fm_canary cookie, so gateway-routed traffic can't forge it.
    # East-west / direct-to-backend calls are still presence-only telemetry
    # (a peer inside the mesh could set it) — never authz.
    "x-fm-canary",
)


@dataclass(frozen=True)
class RequestContext:
    principal: Principal | None = None
    peer: Peer | None = None
    # repr=False: trace_headers may hold the secret x-fm-canary token value, so
    # keep it out of any accidental repr(ctx) / f-string dump.
    trace_headers: dict[str, str] = field(default_factory=dict, repr=False)
    request_id: str = ""
    # Best-effort telemetry hint set from x-fm-canary PRESENCE only (never the
    # raw value). The gateway strips+re-injects it (canary-cookie-gate.yaml), so
    # gateway-routed traffic can't forge it; east-west remains spoofable and this
    # is telemetry-only, never authz.
    is_canary: bool = False


_current: ContextVar[RequestContext | None] = ContextVar("fm_request_context", default=None)


def set_context(ctx: RequestContext) -> object:
    return _current.set(ctx)


def reset_context(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


def current_context() -> RequestContext | None:
    return _current.get()


def current_principal() -> Principal | None:
    ctx = _current.get()
    return ctx.principal if ctx else None


def current_trace_headers() -> dict[str, str]:
    ctx = _current.get()
    return dict(ctx.trace_headers) if ctx else {}
