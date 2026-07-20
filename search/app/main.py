from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401 — register ORM metadata
from app.config import get_settings
from app.database import init_db
from app.routers import search


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router)


@app.get("/api/search/health")
async def health() -> dict[str, object]:
    current = get_settings()
    return {
        "status": "ok",
        "leads_backend_url": current.leads_backend_url,
    }
