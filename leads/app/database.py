from collections.abc import AsyncGenerator

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

_client: AsyncIOMotorClient | None = None


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


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def get_database() -> AsyncGenerator[AsyncIOMotorDatabase, None]:
    yield get_db()
