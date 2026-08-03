"""Optional, gated OpenTelemetry / Pydantic Logfire tracing.

This is the ONE piece of application-level tracing in the platform. Backend
tracing is otherwise Envoy-sidecar-only (the mesh emits spans); this module lets
a service *also* emit fine-grained in-process spans (FastAPI request handling,
outbound httpx, and — instrumented by the service itself, never here — LLM
calls), stitched under the same trace id as the mesh/Faro spans via the inbound
W3C ``traceparent`` that ``PrincipalMiddleware`` already forwards.

Load-bearing safety properties (P6/P10 — this ships into every backend):

- **Off by default, zero cost in prod.** ``configure_tracing`` is a hard no-op
  unless ``FM_LOGFIRE == "1"``. When it is unset (prod), ``logfire`` is *never
  imported* — the import lives lazily inside the guard, so a service that does
  not opt in pays nothing and need not even install the dependency.
- **Optional dependency.** ``logfire`` + the OTLP gRPC exporter are the
  ``tracing`` extra of ``fm-runtime`` (see pyproject). A service opts in by
  installing ``fm-runtime[tracing]`` AND setting ``FM_LOGFIRE=1``. If the flag
  is set but the extra is missing, we log a warning and stay a no-op rather than
  crash the service.
- **Double export safety for the cloud.** ``send_to_logfire='if-token-present'``
  means spans go to Logfire cloud only when ``LOGFIRE_TOKEN`` is set; with no
  token, Logfire is configured but exports nothing. Combined with the
  ``FM_LOGFIRE`` gate, prod would have to *both* set the flag and hold a token
  to ever ship a span off-box.
- **Dual export to the existing Tempo (gRPC only).** When
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (e.g. ``http://tempo.monitoring.svc:4317``),
  *spans* are additionally batched to that OTLP/**gRPC** collector, so LLM+HTTP app
  spans and the mesh spans share one trace id in Tempo. We **pop** the env var
  before ``logfire.configure`` so Logfire does not *also* auto-add its own OTLP/
  **HTTP** exporter (traces AND metrics) to the same endpoint — Tempo's ingress
  admits only the gRPC port :4317, so an HTTP export there 415s and Tempo ingests
  no metrics at all. Only our explicit gRPC *span* processor talks to Tempo.

This module intentionally does NOT instrument ``pydantic-ai``: ``fm_runtime``
must not depend on it. The ``agents`` service calls
``logfire.instrument_pydantic_ai()`` itself after ``install()``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def tracing_enabled() -> bool:
    """True only when the operator has explicitly opted in (``FM_LOGFIRE=1``).

    Kept as a tiny public predicate so callers (and tests) can gate on the exact
    same condition ``configure_tracing`` uses, without importing ``logfire``."""
    return os.environ.get("FM_LOGFIRE", "").strip() == "1"


def configure_tracing(app: Any, *, service_name: str, variant: str) -> bool:
    """Configure Logfire/OTel tracing for ``app`` — a no-op unless opted in.

    Returns True if tracing was configured, False if it was skipped (the common
    case: ``FM_LOGFIRE`` unset, i.e. prod and every non-opted-in service).

    ``variant`` is the deployment variant (``stable``/``canary``) and is recorded
    as the OTel ``environment`` so canary spans are filterable in Logfire/Tempo,
    mirroring the ``variant`` log label.
    """
    if not tracing_enabled():
        # Prod path: logfire is NEVER imported here. Zero cost, no dependency.
        return False

    # Everything below runs only when FM_LOGFIRE=1. Import lazily so the base
    # install path (and prod) never pulls logfire into sys.modules.
    try:
        import logfire
    except ImportError:
        logger.warning(
            "FM_LOGFIRE=1 but the 'tracing' extra is not installed; "
            "tracing disabled. Install fm-runtime[tracing] to enable it."
        )
        return False

    additional_span_processors: list[Any] = []
    # POP, not get: removing this before logfire.configure() stops Logfire from
    # ALSO auto-configuring its own OTLP/HTTP exporter (for traces AND metrics) to
    # this endpoint from the standard OTEL_* env. Tempo's ingress admits only the
    # gRPC port :4317 (HTTP there 415s) and Tempo takes no metrics — we want exactly
    # one gRPC *span* exporter, added explicitly below.
    otlp_endpoint = os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if otlp_endpoint:
        # Dual export: ship spans to the existing Tempo over OTLP/gRPC so app spans
        # share a trace id with the mesh spans without relying on Logfire cloud.
        # Imported lazily alongside logfire; both are in the 'tracing' extra.
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # gRPC target is host:port; strip any scheme and force insecure
            # (in-cluster plaintext to Tempo:4317, same transport the mesh uses).
            grpc_target = otlp_endpoint.removeprefix("http://").removeprefix(
                "https://"
            )
            additional_span_processors.append(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=grpc_target, insecure=True)
                )
            )
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT=%s set but the OTLP gRPC exporter is "
                "not installed; Logfire export only. Install fm-runtime[tracing].",
                otlp_endpoint,
            )

    # send_to_logfire='if-token-present': export to Logfire cloud only when
    # LOGFIRE_TOKEN is set; otherwise configure-but-don't-export. Double safety
    # with the FM_LOGFIRE gate for prod. console=False: never write span noise to
    # stdout (structured JSON logging owns stdout).
    logfire.configure(
        send_to_logfire="if-token-present",
        service_name=service_name,
        environment=variant,
        console=False,
        additional_span_processors=additional_span_processors or None,
    )

    # logfire.configure installs a W3C TraceContext propagator globally, and
    # instrument_fastapi extracts the inbound traceparent per request, so app
    # spans parent under the mesh/Faro trace that fm_runtime already forwards.
    logfire.instrument_fastapi(app)
    logfire.instrument_httpx()

    logger.info(
        "fm_runtime tracing enabled (service=%s variant=%s logfire=if-token-present "
        "otlp=%s)",
        service_name,
        variant,
        otlp_endpoint or "off",
    )
    return True
