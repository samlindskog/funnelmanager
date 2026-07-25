from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from fm_runtime import anonymous, install

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
