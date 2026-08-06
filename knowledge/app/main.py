from contextlib import asynccontextmanager

from fastapi import FastAPI
from fm_runtime import anonymous, install
from sqlalchemy import text

from app.config import get_settings
from app.database import engine, init_db
from app.graph import graph_service
from app.routers import mcp as mcp_router
from app.sync import sync_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    # Graph + LLM are OPTIONAL at startup (leads-Milvus tolerance pattern):
    # a missing Neo4j or OPENAI_API_KEY degrades graph/fuzzy features with a
    # clear reason in /stats, but never blocks the service — this keeps the
    # prod rollout safe before the graph store's capacity work lands.
    try:
        await graph_service.ensure()
    except RuntimeError:
        pass  # reason is logged + surfaced via /stats
    sync_manager.start()
    try:
        yield
    finally:
        await sync_manager.stop()


async def _db_ready() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

# fm_runtime installs PrincipalMiddleware (audience `knowledge`), structured
# logging, /healthz, /readyz, /metrics, and /api/knowledge/whoami.
install(app, service="knowledge", ready_checks={"postgres": _db_ready})

app.include_router(mcp_router.router)


@app.get("/api/knowledge/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def health() -> dict[str, str]:
    return {"status": "ok"}
