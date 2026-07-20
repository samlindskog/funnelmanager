"""Request-scoped context: principal, peer workload, trace headers.

Set by PrincipalMiddleware at ingress; read anywhere (handlers, log
formatter, outbound InternalClient) without threading arguments through every
call. Detached background jobs run outside this context — they capture what
they need (see InternalClient.detached)."""

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
)


@dataclass(frozen=True)
class RequestContext:
    principal: Principal | None = None
    peer: Peer | None = None
    trace_headers: dict[str, str] = field(default_factory=dict)
    request_id: str = ""


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
