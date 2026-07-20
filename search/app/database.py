from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def _drop_column_if_exists(conn, table: str, column: str) -> None:
    exists = await conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table
              AND column_name = :column
            """
        ),
        {"table": table, "column": column},
    )
    if exists.scalar() is not None:
        await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


async def init_db() -> None:
    # Import models so metadata is registered before create_all.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Removed after switching to full eager ingest into search_results.
        await _drop_column_if_exists(conn, "search_history", "apollo_page_fetched")
        await _drop_column_if_exists(conn, "search_history", "apollo_exhausted")
        # Payloads live in Mongo via leads; Postgres only stores Apollo ids.
        await _drop_column_if_exists(conn, "search_results", "result_json")
