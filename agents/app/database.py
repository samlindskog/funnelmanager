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
    # The approval tables were re-keyed run_id -> (session_id, turn_id). On a db
    # that pre-existed with the old run-based shape, ADD the new key columns
    # (defaulted so the old shape's rows/inserts still satisfy them) and RELAX the
    # legacy run_id NOT NULL (new inserts don't set it). Idempotent + best-effort.
    "ALTER TABLE agent_pending_approvals ADD COLUMN IF NOT EXISTS "
    "session_id VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE agent_pending_approvals ADD COLUMN IF NOT EXISTS "
    "turn_id VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE agent_pending_approvals ALTER COLUMN run_id DROP NOT NULL",
    "ALTER TABLE agent_consumed_approvals ADD COLUMN IF NOT EXISTS "
    "session_id VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE agent_consumed_approvals ALTER COLUMN run_id DROP NOT NULL",
)


async def _heal_schema() -> None:
    """Additively bring a pre-existing schema up to the current model. Scoped to
    known-drifted columns; not a general schema sync.

    Each statement runs in its OWN transaction so a failing ALTER (e.g. dropping
    NOT NULL on the legacy ``run_id`` column, which does NOT exist on a fresh
    ``create_all`` schema) aborts only itself. Running them in the same
    transaction as ``create_all`` would poison that transaction on the first such
    error — Postgres aborts the whole tx — and roll every table back, so a fresh
    DB would boot with **zero** tables. Isolating per statement keeps the heal
    best-effort without ever endangering ``create_all``.
    """
    for ddl in _COLUMN_HEALS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
        except Exception:  # noqa: BLE001 — a column/table may not exist; heal is best-effort
            pass


async def _create_all() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def init_db() -> None:
    # Import models so metadata is registered before create_all.
    from app import models  # noqa: F401

    try:
        await _create_all()
    except Exception:
        # Most likely the database itself is missing (first boot of this
        # service on an existing Postgres volume) — create it and retry once.
        await _create_database_if_missing()
        await _create_all()
    # Heal AFTER (and separate from) create_all, per-statement isolated — never in
    # the create_all transaction, or a failing heal would roll the tables back.
    await _heal_schema()
