import logging
from collections.abc import AsyncGenerator

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None

# Negative-resolve cache: domains Apollo genuinely has no organization for, so a
# workflow re-upload short-circuits without re-burning an Apollo org-search credit.
RESOLVE_MISSES_COLLECTION = "resolve_misses"


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncIOMotorClient(settings.mongodb_url)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    settings = get_settings()
    return get_client()[settings.mongodb_db]


async def init_db() -> None:
    db = get_db()
    # Drop legacy unique indexes from the previous document shape.
    for legacy_index in ("apollo_person_id_1", "apollo_person_id", "follow_up_1"):
        try:
            await db.leads.drop_index(legacy_index)
        except Exception:
            pass
    # Unique Apollo id prevents duplicate person/organization documents.
    await db.leads.create_index("apollo_id", unique=True)
    await db.leads.create_index("entity_type")
    await db.leads.create_index("embedding")
    await db.leads.create_index("apollo_enriched.linkedin")
    await db.leads.create_index("apollo_enriched.email")
    await db.leads.create_index("apollo_enriched.phone")
    await db.leads.create_index("updated_at")
    # Derived top-level index fields (semantic-search v2): support company filtering
    # and email/phone/linkedin exists-filters ({"$ne": None} / {"$eq": None}).
    await db.leads.create_index("company_id")
    await db.leads.create_index("company_apollo_id")
    await db.leads.create_index("email")
    await db.leads.create_index("phone")
    await db.leads.create_index("linkedin")
    await db.leads.create_index("domain")
    # Multikey index on learned alias domains (an org resolvable by a queried domain
    # that differs from its canonical ``domain``) — backs the resolve alias lookup.
    await db.leads.create_index("alias_domains")
    await _ensure_resolve_misses_indexes(db)


async def _ensure_resolve_misses_indexes(db: AsyncIOMotorDatabase) -> None:
    """Unique(value) + TTL(created_at) on the negative-resolve cache.

    Log-and-tolerate (like the Milvus ensure): a missing/failed index degrades the
    cache to a no-op, never blocks startup. The TTL comes from
    ``resolve_miss_ttl_seconds``; changing it later conflicts with the existing
    index (logged, old TTL retained) — recreate the index by hand to change it.
    """
    ttl = int(get_settings().resolve_miss_ttl_seconds)
    misses = db[RESOLVE_MISSES_COLLECTION]
    try:
        await misses.create_index("value", unique=True)
        await misses.create_index("created_at", expireAfterSeconds=ttl)
    except Exception:
        logger.exception(
            "resolve_misses index ensure failed; negative-resolve cache disabled"
        )


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def get_database() -> AsyncGenerator[AsyncIOMotorDatabase, None]:
    yield get_db()
