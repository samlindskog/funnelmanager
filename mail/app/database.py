from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.engine import make_url
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


async def _create_database_if_missing() -> None:
    """CREATE DATABASE for this service when it does not exist yet.

    Postgres runs /docker-entrypoint-initdb.d only on a fresh volume, so an
    existing deployment would never grow the mail database — create it from
    here against the admin database instead (CREATE DATABASE cannot run in a
    transaction, hence AUTOCOMMIT).
    """
    url = make_url(settings.database_url)
    admin_engine = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if exists.scalar() is None:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin_engine.dispose()


async def init_db() -> None:
    # Import models so metadata is registered before create_all.
    from app import models  # noqa: F401

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        # Most likely the database itself is missing (first boot of this
        # service on an existing Postgres volume) — create it and retry once.
        await _create_database_if_missing()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
