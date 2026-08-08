import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from fm_runtime import anonymous, get_runtime_settings, install, tracing_enabled

from app import models  # noqa: F401 — register ORM metadata
from app.config import get_settings
from app.database import engine, init_db
from app.routers import internal_jobs, sessions
from app.runner import turn_runner
from app.scheduler import agent_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    # Start the in-process schedule poller on the PRIMARY pod only. The poller's
    # captured-token cache and serial-chat guard are per-pod state, so it must be a
    # single writer: the agents-canary shares this prod agents-db but must NOT run a
    # competing scheduler (it exists to trace request flows). Fail-safe — default
    # variant is "stable", so an unset FM_DEPLOYMENT_VARIANT still runs the poller;
    # only an explicit `canary` pod skips it. (The atomic DB claim in
    # schedules.claim_due_schedule still guards a brief two-poller rollout overlap.)
    is_canary = get_runtime_settings().deployment_variant == "canary"
    if get_settings().should_run_scheduler(is_canary=is_canary):
        await agent_scheduler.start()
        logger.info("agents: schedule poller started (variant=%s)", "canary" if is_canary else "stable")
    else:
        logger.info("agents: schedule poller NOT started (single-writer; variant=%s)", "canary" if is_canary else "stable")
    yield
    # Stop the poller (no-op if it never started), then cancel any in-flight
    # runtime-agent turns on shutdown so tasks don't leak.
    await agent_scheduler.stop()
    await turn_runner.shutdown()


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

# The human-facing sessions API (/api/agents, gated by the agents-access role) and
# the internal jobs producer surface (/internal/jobs/v1, gated by jobs-internal).
app.include_router(sessions.router)
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
