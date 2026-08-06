"""Dedicated funnelmanager_knowledge store. The engine is lazy (no connection at
import), so ``import app.main`` succeeds without a database — init_db runs in
the lifespan."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings


def _make_engine() -> AsyncEngine:
    url = get_settings().database_url
    # CNPG's generated secret has a plain postgresql:// URI — force asyncpg.
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return create_async_engine(url, pool_pre_ping=True)


engine = _make_engine()
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from app.models import Base

    async with engine.begin() as conn:
        # pgvector is a TRUSTED extension (>= 0.5), so the db owner may create
        # it without superuser — but the .so must exist in the server image
        # (dev compose: pgvector/pgvector:pg16; k3s: see knowledge-db
        # cluster.yaml). Failing here fails /readyz, which is correct: the
        # records store is this service's core surface.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
