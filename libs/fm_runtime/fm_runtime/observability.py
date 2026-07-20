"""Health + metrics endpoints, identical on every service.

- GET /healthz  — liveness: process is up; always 200. (kubelet probe)
- GET /readyz   — readiness: runs registered dependency checks (DB ping,
                  broker reachability, ...); any failure → 503 with detail.
- GET /metrics  — Prometheus exposition (default registry: the runtime's
                  fm_http_* series plus process/python collectors).

All three are cluster-internal (probes + Prometheus scrapes bypass the
gateway) and are annotated @anonymous so the principal middleware admits
kubelet/Prometheus, which carry no JWT."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fm_runtime.annotations import anonymous

logger = logging.getLogger(__name__)

ReadyCheck = Callable[[], Awaitable[None]]

_CHECK_TIMEOUT_SECONDS = 3.0


def install_observability(app: Any, ready_checks: dict[str, ReadyCheck] | None = None) -> None:
    """Add /healthz, /readyz, /metrics to a FastAPI app."""
    checks = ready_checks or {}

    @app.get("/healthz", include_in_schema=False)
    @anonymous("liveness probe (kubelet, cluster-internal)")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    @anonymous("readiness probe (kubelet, cluster-internal)")
    async def readyz() -> Response:
        failures: dict[str, str] = {}
        for name, check in checks.items():
            try:
                await asyncio.wait_for(check(), timeout=_CHECK_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 — any failure means not ready
                failures[name] = f"{type(exc).__name__}: {exc}"
        if failures:
            logger.warning("Readiness failing: %s", failures)
            return JSONResponse({"status": "unready", "failures": failures}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics", include_in_schema=False)
    @anonymous("Prometheus scrape (cluster-internal)")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
