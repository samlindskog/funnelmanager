"""Anonymous-endpoint annotation — the single source of truth for the
public-anonymous allowlist.

Usage on a FastAPI route:

    @router.post("/api/leads/webhooks/apollo")
    @anonymous("Apollo cannot send bearer tokens; secret-in-path auth")
    async def apollo_webhook(...): ...

Effects:
- PrincipalMiddleware lets the request through without a principal.
- The route's OpenAPI operation gains `x-public-anonymous: {reason}`.
- `collect_anonymous(app)` exports the machine-readable allowlist that the
  OPA policy data (Phase 2) is generated from — code and policy cannot
  drift apart.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_ATTR = "__fm_anonymous__"


def anonymous(reason: str) -> Callable:
    """Mark an endpoint as callable without a principal. A reason is
    mandatory — it becomes the security-review justification."""

    def mark(fn: Callable) -> Callable:
        setattr(fn, _ATTR, reason)
        return fn

    return mark


def is_anonymous(endpoint: Callable) -> bool:
    return hasattr(endpoint, _ATTR)


def _route_regex(path_format: str) -> re.Pattern[str]:
    # /api/leads/webhooks/apollo/{secret} -> ^/api/leads/webhooks/apollo/[^/]+$
    pattern = re.sub(r"\{[^}/]+:path\}", ".+", path_format)
    pattern = re.sub(r"\{[^}/]+\}", "[^/]+", pattern)
    return re.compile(f"^{pattern}$")


def build_matchers(app: Any) -> list[tuple[frozenset[str], re.Pattern[str]]]:
    """Compile (methods, path-regex) matchers for every annotated route.
    Called lazily by the middleware once routing tables are final."""
    matchers: list[tuple[frozenset[str], re.Pattern[str]]] = []
    for route in getattr(app, "routes", []):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not is_anonymous(endpoint):
            continue
        methods = frozenset(getattr(route, "methods", None) or {"GET"})
        matchers.append((methods, _route_regex(route.path_format)))
        # Surface the annotation in the OpenAPI document.
        openapi_extra = getattr(route, "openapi_extra", None)
        if openapi_extra is not None or hasattr(route, "openapi_extra"):
            extra = dict(openapi_extra or {})
            extra["x-public-anonymous"] = getattr(endpoint, _ATTR)
            route.openapi_extra = extra
    return matchers


def collect_anonymous(app: Any, service: str) -> list[dict[str, str]]:
    """Export the allowlist: [{service, method, path, reason}]."""
    entries: list[dict[str, str]] = []
    for route in getattr(app, "routes", []):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not is_anonymous(endpoint):
            continue
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            entries.append(
                {
                    "service": service,
                    "method": method,
                    "path": route.path_format,
                    "reason": getattr(endpoint, _ATTR),
                }
            )
    return entries
