"""Combined semantic-search-v2 migration: backfill derived fields + rebuild Milvus.

Run inside the leads container (CWD /app):

    docker compose ... exec leads python scripts/reembed.py

Pass 1 — backfill the derived top-level fields (name / title / company_id /
email / phone / linkedin) on **every** lead doc (not just embedded ones), so
existing docs gain the fields new writes already carry. Only non-null derived
keys are ``$set`` (a field is never regressed to null).

Pass 2 — drop and recreate the (v2) Milvus collection, then re-embed every lead
whose Mongo doc has ``embedding: true`` into all applicable per-kind rows
(apollo / name / title). Mongo is the source of truth, so this is safe to run
after seeding Mongo on a fresh box or any time the vector store needs rebuilding.
Idempotent.

Mongo's ``embedding_source`` precedence is left untouched (Milvus stores no
precedence), so a high ``source_precedence`` is passed purely to keep
``index_lead_docs`` from skipping any doc during a from-scratch rebuild.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

# Runnable as `python scripts/reembed.py` from the service root (the documented
# operator invocation) — put the service root on sys.path for the app imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from pymilvus import utility

from app.apollo_endpoints import responses_from_doc
from app.config import get_settings
from app.derived import derive_top_fields
from app.milvus_client import connect_milvus, ensure_collection_async, index_lead_docs

BATCH = 128
# Larger than any real endpoint precedence, so nothing is skipped on rebuild.
SOURCE_PRECEDENCE = 10_000


async def _flush(batch: list[dict]) -> int:
    if not batch:
        return 0
    return len(await index_lead_docs(batch, source_precedence=SOURCE_PRECEDENCE))


async def _backfill_top_fields(db) -> int:
    """Pass 1: derive + write top-level fields on every doc. Returns docs updated."""
    total = await db.leads.count_documents({})
    print(f"pass 1: backfilling derived top-level fields on {total} doc(s)", flush=True)
    updated = 0
    scanned = 0
    projection = {
        "entity_type": 1,
        "apollo_responses": 1,
        "apollo_response": 1,
        "apollo_enriched": 1,
        "updated_at": 1,
        "created_at": 1,
    }
    async for doc in db.leads.find({}, projection=projection):
        scanned += 1
        entity_type = doc.get("entity_type") or "person"
        derived = derive_top_fields(entity_type, responses_from_doc(doc))
        # Always stamp derived_at (the v2 authoritative-top-fields marker), even
        # when no derived field is present, so downstream consumers can trust the
        # top-level fields without proxying off name-presence.
        await db.leads.update_one(
            {"_id": doc["_id"]},
            {"$set": {**derived, "derived_at": datetime.now(timezone.utc)}},
        )
        updated += 1
        if scanned % 5000 == 0:
            print(f"  scanned {scanned}/{total} (updated {updated})", flush=True)
    print(f"pass 1 DONE: updated {updated}/{total} doc(s)", flush=True)
    return updated


async def _rebuild_embeddings(db, cfg) -> int:
    """Pass 2: drop/recreate the v2 collection and re-embed embedded docs."""
    connect_milvus(cfg)
    name = cfg.milvus_collection
    if utility.has_collection(name):
        utility.drop_collection(name)
        print(f"pass 2: dropped existing collection {name}", flush=True)
    await ensure_collection_async(cfg)

    total = await db.leads.count_documents({"embedding": True})
    print(f"pass 2: {total} lead(s) marked embedding=True", flush=True)

    indexed = 0
    batch: list[dict] = []
    async for doc in db.leads.find({"embedding": True}):
        batch.append(doc)
        if len(batch) >= BATCH:
            indexed += await _flush(batch)
            batch = []
            print(f"  indexed {indexed}/{total}", flush=True)
    indexed += await _flush(batch)
    print(f"pass 2 DONE: {indexed} doc(s) re-embedded into {name}", flush=True)
    return indexed


async def main() -> None:
    cfg = get_settings()
    # Fail fast before touching anything: the migration is a single unit (pass 1
    # backfill + pass 2 Milvus rebuild). Skipping pass 2 would look like success
    # while leaving the vector store un-rebuilt. An operator fixes the key and
    # re-runs (both passes are idempotent).
    if not cfg.openai_configured:
        raise SystemExit("OPENAI_API_KEY not configured; cannot embed.")

    client = AsyncIOMotorClient(cfg.mongodb_url)
    db = client[cfg.mongodb_db]

    await _backfill_top_fields(db)
    await _rebuild_embeddings(db, cfg)


if __name__ == "__main__":
    asyncio.run(main())
