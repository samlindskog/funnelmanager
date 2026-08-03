#!/usr/bin/env python3
"""Standalone Logfire + pydantic-ai smoke test for the `agents` service.

WHAT IT PROVES
    That a real runtime-agent model call is captured as a Logfire span end to
    end: it mirrors exactly what production does — the tracing setup that
    ``fm_runtime.install()`` -> ``configure_tracing()`` performs, PLUS the
    ``logfire.instrument_pydantic_ai()`` call that ``agents/app/main.py`` adds —
    then runs ONE trivial ``pydantic_ai.Agent.run`` against OpenAI and flushes.

WHO RUNS IT
    The OPERATOR, by hand, with live credentials. It is NOT run in CI and the
    build agent does not run it (it needs a real OpenAI key and spends a few
    tokens). After it prints OK, confirm the span landed in Logfire via the
    query API, e.g.:

        curl -H "Authorization: Bearer $LOGFIRE_READ_TOKEN" \
             'https://logfire-api.pydantic.dev/v1/query' \
             --data-urlencode "sql=select trace_id, span_name, service_name
                 from records where service_name = 'agents-smoke'
                 order by start_timestamp desc limit 10"

USAGE (from the agents/ directory, its venv active)
        export OPENAI_API_KEY=sk-...          # required — a real key is spent
        export LOGFIRE_TOKEN=pylf_v1_...      # required to ship to Logfire cloud
        # optional dual export to a local/remote Tempo (OTLP gRPC):
        # export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
        python scripts/logfire_smoke.py

    With no LOGFIRE_TOKEN it still runs and instruments (spans are built) but
    nothing is exported to the cloud — set the token to actually verify a span
    lands.
"""

from __future__ import annotations

import asyncio
import os
import sys

# This script's whole purpose is telemetry, so opt in — mirrors the runtime
# FM_LOGFIRE=1 path (config below configures Logfire directly regardless).
os.environ.setdefault("FM_LOGFIRE", "1")

# Run from agents/ so absolute `app.*` imports resolve (matches the service CWD).
SERVICE_NAME = "agents-smoke"


def _configure_tracing() -> bool:
    """Mirror fm_runtime.tracing.configure_tracing (minus FastAPI wiring) so the
    smoke test exercises the exact same Logfire config the service uses, then add
    the pydantic-ai instrumentation app/main.py contributes. Returns True on the
    happy path."""
    try:
        import logfire
    except ImportError:
        print(
            "logfire is not installed. Install the agents telemetry deps:\n"
            "    pip install -r requirements.txt\n"
            "(they carry logfire[fastapi,httpx] + the OTLP gRPC exporter).",
            file=sys.stderr,
        )
        return False

    additional_span_processors = None
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            additional_span_processors = [
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            ]
            print(f"dual export -> OTLP/gRPC {otlp_endpoint}")
        except ImportError:
            print(
                "OTEL_EXPORTER_OTLP_ENDPOINT set but the OTLP gRPC exporter is "
                "missing; Logfire export only.",
                file=sys.stderr,
            )

    # Same knobs as configure_tracing: cloud export only when a token is present,
    # no console span noise.
    logfire.configure(
        send_to_logfire="if-token-present",
        service_name=SERVICE_NAME,
        environment="smoke",
        console=False,
        additional_span_processors=additional_span_processors,
    )
    # The line agents/app/main.py adds that fm_runtime deliberately cannot: it
    # can't depend on pydantic-ai. This is what makes the Agent.run below a span.
    logfire.instrument_pydantic_ai()

    if not os.environ.get("LOGFIRE_TOKEN", "").strip():
        print(
            "WARNING: LOGFIRE_TOKEN unset — instrumentation is active but nothing "
            "is exported to Logfire cloud, so you can't query the span. Set it to "
            "actually verify.",
            file=sys.stderr,
        )
    return True


async def _run_once() -> str:
    """Build the same model the runtime agent uses and run one trivial prompt."""
    from pydantic_ai import Agent

    from app.config import get_settings
    from app.runner import build_model

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is unset — a real key is required.", file=sys.stderr)
        raise SystemExit(2)

    agent = Agent(
        build_model(settings),
        system_prompt="You are a smoke test. Answer in one short word.",
    )
    result = await agent.run("Reply with the single word: pong")
    return result.output


def main() -> int:
    if not _configure_tracing():
        return 1

    import logfire

    with logfire.span("logfire_smoke"):
        output = asyncio.run(_run_once())

    # Ensure buffered spans are shipped before the process exits.
    logfire.force_flush()
    print(f"OK — model replied: {output!r}")
    print(
        "A span named 'logfire_smoke' plus pydantic-ai model/tool spans were "
        f"emitted under service_name='{SERVICE_NAME}'. Query Logfire to confirm."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
