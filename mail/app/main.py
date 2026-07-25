from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from fm_runtime import anonymous, install

from app import models  # noqa: F401 — register ORM metadata
from app.campaigns import campaign_manager
from app.config import get_settings
from app.database import engine, init_db
from app.routers import mail, mcp
from app.sync import sync_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    sync_manager.start()
    campaign_manager.start()
    yield
    await campaign_manager.stop()
    await sync_manager.stop()


async def _db_ready() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

install(app, service="mail", ready_checks={"postgres": _db_ready})

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mail.router)
app.include_router(mcp.router)


@app.get("/api/mail/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def health() -> dict[str, object]:
    current = get_settings()
    return {
        "status": "ok",
        "oauth_configured": current.oauth_configured,
    }
