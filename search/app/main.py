from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fm_runtime import anonymous, install
from sqlalchemy import text

from app import models  # noqa: F401 — register ORM metadata
from app.config import get_settings
from app.database import engine, init_db
from app.routers import internal_jobs, mcp, search


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


async def _db_ready() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

install(app, service="search", ready_checks={"postgres": _db_ready})

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router)
# MCP-facing surface (/api/search/mcp/v1/*) — distinct handlers from the UI routes.
app.include_router(mcp.router)
# Producer side of the jobs contract (/internal/jobs/v1/*) — jobs service only
# (jobs-internal role); not nginx-routed.
app.include_router(internal_jobs.router)


@app.get("/api/search/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def health() -> dict[str, object]:
    current = get_settings()
    return {
        "status": "ok",
        "leads_backend_url": current.leads_backend_url,
    }
