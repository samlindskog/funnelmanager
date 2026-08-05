from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _async_url(url: str) -> str:
    """Force the asyncpg driver: CNPG's generated connection secrets (and most
    external tooling) hand out plain postgresql:// URLs."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


settings = get_settings()
engine = create_async_engine(_async_url(settings.database_url), echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def _create_database_if_missing() -> None:
    """CREATE DATABASE for this service when it does not exist yet.

    Postgres runs /docker-entrypoint-initdb.d only on a fresh volume, so an
    existing deployment would never grow the agents database — create it from
    here against the admin database instead (CREATE DATABASE cannot run in a
    transaction, hence AUTOCOMMIT). Mirrors the mail/jobs services.
    """
    url = make_url(_async_url(settings.database_url))
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


# Columns added to the session/message/approval tables after their initial
# release. ``create_all`` only creates missing *tables*, never ALTERs an existing
# one, and there is no migration framework here — so an existing agents-db (e.g. a
# prior canary that ran an earlier cut of these tables) keeps the old shape and a
# SELECT of a new column 500s. Postgres ``ADD COLUMN IF NOT EXISTS`` is idempotent,
# so healing on every boot is safe and needs no manual DB surgery. Types/defaults
# mirror app.models. (mail's pattern.)
_COLUMN_HEALS = (
    "ALTER TABLE agent_session ADD COLUMN IF NOT EXISTS last_error TEXT",
    "ALTER TABLE agent_session ADD COLUMN IF NOT EXISTS "
    "context_watermark INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agent_message ADD COLUMN IF NOT EXISTS usage JSONB",
    "ALTER TABLE agent_message ADD COLUMN IF NOT EXISTS model VARCHAR(128)",
    "ALTER TABLE agent_consumed_approvals ADD COLUMN IF NOT EXISTS "
    "turn_id VARCHAR(64) NOT NULL DEFAULT ''",
)


async def _heal_schema(conn) -> None:
    """Additively bring a pre-existing schema up to the current model. Scoped to
    known-drifted columns; not a general schema sync. Best-effort per statement so
    one missing base table never blocks the rest."""
    for ddl in _COLUMN_HEALS:
        try:
            await conn.execute(text(ddl))
        except Exception:  # noqa: BLE001 — a table may not exist yet on first boot
            pass


async def init_db() -> None:
    # Import models so metadata is registered before create_all.
    from app import models  # noqa: F401

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _heal_schema(conn)
    except Exception:
        # Most likely the database itself is missing (first boot of this
        # service on an existing Postgres volume) — create it and retry once.
        await _create_database_if_missing()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _heal_schema(conn)
