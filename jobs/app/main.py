from contextlib import asynccontextmanager

from fastapi import FastAPI
from fm_runtime import anonymous, install
from sqlalchemy import text

from app import models  # noqa: F401 — register ORM metadata
from app.config import get_settings
from app.database import engine, init_db
from app.routers import mcp
from app.subscriber import subscriber_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    # Start the config-driven producer subscribers. They tolerate a producer
    # being absent/unreachable (v1 producers search/agents may not exist yet) —
    # a failed connection reconnects with backoff and never crashes the app.
    subscriber_manager.start()
    yield
    await subscriber_manager.stop()


async def _db_ready() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

install(app, service="jobs", ready_checks={"postgres": _db_ready})

# No CORS middleware: jobs binds loopback only, is never nginx-routed, and has
# no browser surface (MCP + internal producers are the only callers).

app.include_router(mcp.router)


@app.get("/api/jobs/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def health() -> dict[str, object]:
    current = get_settings()
    return {
        "status": "ok",
        "producers": list(current.producer_map),
        "subscriber_enabled": current.subscriber_enabled,
    }
