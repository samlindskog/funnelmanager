from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _async_url(url: str) -> str:
    """Force the asyncpg driver: CNPG's generated connection secrets (and
    most external tooling) hand out plain postgresql:// URLs."""
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


# Columns added to the pre-existing ``mail_accounts`` table after its initial
# release (Phase 5A). ``create_all`` only creates missing *tables*, never ALTERs
# an existing one, and there is no migration framework here — so an upgraded
# deployment keeps the old table shape and any SELECT of these columns 500s.
# Postgres ``ADD COLUMN IF NOT EXISTS`` is idempotent, so healing on every boot
# is safe and needs no manual DB surgery. Types/defaults mirror app.models.
_ACCOUNT_COLUMN_HEALS = (
    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS "
    "backfill_authorized BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS "
    "backup_estimate_bytes BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS "
    "messages_total INTEGER NOT NULL DEFAULT 0",
)


async def _heal_schema(conn) -> None:
    """Additively bring a pre-existing schema up to the current model. Scoped
    strictly to the known-drifted ``mail_accounts`` columns; this is not a
    general schema sync."""
    for ddl in _ACCOUNT_COLUMN_HEALS:
        await conn.execute(text(ddl))


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
