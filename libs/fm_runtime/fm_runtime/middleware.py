"""Pure-ASGI middleware: principal extraction + access log + HTTP metrics.

Deliberately NOT Starlette's BaseHTTPMiddleware — that wraps the downstream
app in a buffered task, which this repo's NDJSON streaming endpoints cannot
tolerate. This middleware never touches response bodies.

Behavior per request:
1. Parse the forwarded JWT (verify locally only when FM_JWT_VERIFY=true —
   in-mesh Istio has already validated it).
2. Stash Principal/Peer/trace headers in a contextvar + `request.state`.
3. If the route is not annotated @anonymous and there is no valid principal,
   reply 401 (FM_REQUIRE_PRINCIPAL=false downgrades this to allow-through,
   for bring-up debugging only).
4. On response: emit one structured access-log line and record Prometheus
   request count/duration labeled by route template (not raw path).
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from prometheus_client import Counter, Histogram

from fm_runtime import context as fm_context
from fm_runtime.annotations import build_matchers
from fm_runtime.principal import TokenError, parse_peer, parse_token, summarize_for_log
from fm_runtime.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger("fm.access")

HTTP_REQUESTS = Counter(
    "fm_http_requests_total",
    "HTTP requests handled",
    ["service", "method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "fm_http_request_duration_seconds",
    "HTTP request duration",
    ["service", "method", "route"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)


def _header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


class PrincipalMiddleware:
    def __init__(
        self,
        app,
        service: str,
        settings: RuntimeSettings | None = None,
        extra_anonymous: tuple[str, ...] = (),
    ) -> None:
        """extra_anonymous: path regexes admitted without a principal in
        addition to @anonymous-annotated routes — for mounts that carry the
        principal inside their own protocol layer (e.g. the /mcp transport,
        whose tools pass the token as a call argument)."""
        self.app = app
        self.service = service
        self.settings = settings or get_runtime_settings()
        self._anonymous: list[tuple[frozenset[str], re.Pattern[str]]] | None = None
        self._extra = tuple(re.compile(p) for p in extra_anonymous)

    def _is_anonymous(self, scope: dict) -> bool:
        path = scope.get("path", "")
        if any(pattern.match(path) for pattern in self._extra):
            return True
        if self._anonymous is None:
            self._anonymous = build_matchers(scope["app"])
        method = scope.get("method", "GET")
        return any(
            method in methods and pattern.match(path)
            for methods, pattern in self._anonymous
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":  # CORS preflight cannot carry credentials
            await self.app(scope, receive, send)
            return

        principal = None
        token_error: TokenError | None = None
        auth_header = _header(scope, b"authorization") or ""
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            try:
                principal = await parse_token(token.strip(), self.settings)
            except TokenError as exc:
                token_error = exc

        request_id = _header(scope, b"x-request-id") or uuid.uuid4().hex
        trace_headers = {}
        for name in fm_context.TRACE_HEADERS:
            value = _header(scope, name.encode())
            if value:
                trace_headers[name] = value
        trace_headers.setdefault("x-request-id", request_id)

        ctx = fm_context.RequestContext(
            principal=principal,
            peer=parse_peer(_header(scope, b"x-forwarded-client-cert")),
            trace_headers=trace_headers,
            request_id=request_id,
        )
        scope.setdefault("state", {})["fm_context"] = ctx

        if principal is None and not self._is_anonymous(scope):
            if self.settings.require_principal:
                detail = str(token_error) if token_error else "Not authenticated"
                await _send_401(send, detail)
                self._observe(scope, 401, 0.0, ctx)
                return
            if token_error:
                logger.warning("Invalid token allowed through (FM_REQUIRE_PRINCIPAL=false): %s", token_error)

        started = time.perf_counter()
        status_holder = {"status": 0}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        ctx_token = fm_context.set_context(ctx)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            fm_context.reset_context(ctx_token)
            self._observe(scope, status_holder["status"], time.perf_counter() - started, ctx)

    def _observe(self, scope: dict, status: int, elapsed: float, ctx) -> None:
        route = getattr(scope.get("route"), "path_format", None) or scope.get("path", "")
        method = scope.get("method", "GET")
        HTTP_REQUESTS.labels(self.service, method, route, str(status or 0)).inc()
        HTTP_DURATION.labels(self.service, method, route).observe(elapsed)
        logger.info(
            "%s %s %s",
            method,
            scope.get("path", ""),
            status or 0,
            extra={
                "http": {
                    "method": method,
                    "path": scope.get("path", ""),
                    "route": route,
                    "status": status or 0,
                    "duration_ms": round(elapsed * 1000, 2),
                },
                "sub": ctx.principal.sub if ctx.principal else None,
                "principal": summarize_for_log(ctx.principal),
                "peer": ctx.peer.spiffe_id if ctx.peer else None,
                "request_id": ctx.request_id,
            },
        )


async def _send_401(send, detail: str) -> None:
    body = ('{"detail": "' + detail.replace('"', "'") + '"}').encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
