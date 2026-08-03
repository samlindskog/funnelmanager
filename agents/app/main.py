import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from fm_runtime import anonymous, install, tracing_enabled

from app import models  # noqa: F401 — register ORM metadata
from app.config import get_settings
from app.database import engine, init_db
from app.routers import internal_jobs, tasks
from app.runner import run_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield
    # Cancel any in-flight runtime-agent runs on shutdown so tasks don't leak.
    await run_manager.shutdown()


async def _db_ready() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

# fm_runtime installs PrincipalMiddleware (audience `agents`), structured
# logging, /healthz, /readyz, /metrics, and /api/agents/whoami.
install(app, service="agents", ready_checks={"postgres": _db_ready})

# Phase 1 telemetry: fm_runtime.install() already configured Logfire/OTel and
# instrumented FastAPI + httpx (a no-op unless FM_LOGFIRE=1). fm_runtime cannot
# depend on pydantic-ai, so `agents` — the one service that opts in — adds the
# pydantic-ai instrumentation itself: the runtime Agent's model calls, tool
# calls, and token usage become spans stitched under the same trace id. The
# `logfire` import is lazy and gated on the exact predicate configure_tracing
# used, so with FM_LOGFIRE unset (prod) logfire is never imported here.
if tracing_enabled():
    try:
        import logfire  # noqa: PLC0415 — lazy: keep logfire out of the prod import path

        logfire.instrument_pydantic_ai()
    except ImportError:
        # Mirror configure_tracing's contract: FM_LOGFIRE=1 but the
        # fm-runtime[tracing] extra absent -> warn and degrade, never crash boot.
        logging.getLogger("app.telemetry").warning(
            "FM_LOGFIRE=1 but logfire is not installed (fm-runtime[tracing]); "
            "skipping pydantic-ai instrumentation",
        )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The human-facing task API (/api/agents, gated by the agents-access role) and
# the internal jobs producer surface (/internal/jobs/v1, gated by jobs-internal).
app.include_router(tasks.router)
app.include_router(internal_jobs.router)


@app.get("/api/agents/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def health() -> dict[str, object]:
    current = get_settings()
    return {
        "status": "ok",
        "model": current.model_name,
        "mcp_url": current.mcp_url,
        "llm_configured": bool(current.openai_api_key),
    }
