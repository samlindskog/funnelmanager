"""Rebuild the Milvus collection from Mongo docs already marked embedding=True.

Run inside the leads-backend container (CWD /app):

    docker compose ... exec leads-backend python scripts/reembed.py

Drops and recreates the collection, then re-embeds every lead whose Mongo doc
has ``embedding: true``. Mongo is the source of truth, so this is safe to run
after seeding Mongo on a fresh box (avoids copying Milvus' bloated object
storage) or any time the vector store needs to be rebuilt. Idempotent.

Mongo's ``embedding_source`` precedence is left untouched (the Milvus schema
stores no precedence), so a high ``source_precedence`` is passed purely to keep
``index_lead_docs`` from skipping any doc during a from-scratch rebuild.
"""

from __future__ import annotations

import asyncio

from motor.motor_asyncio import AsyncIOMotorClient
from pymilvus import utility

from app.config import get_settings
from app.milvus_client import connect_milvus, ensure_collection_async, index_lead_docs

BATCH = 128
# Larger than any real endpoint precedence, so nothing is skipped on rebuild.
SOURCE_PRECEDENCE = 10_000


async def _flush(batch: list[dict]) -> int:
    if not batch:
        return 0
    return len(await index_lead_docs(batch, source_precedence=SOURCE_PRECEDENCE))


async def main() -> None:
    cfg = get_settings()
    if not cfg.openai_configured:
        raise SystemExit("OPENAI_API_KEY not configured; cannot embed.")

    client = AsyncIOMotorClient(cfg.mongodb_url)
    db = client[cfg.mongodb_db]

    connect_milvus(cfg)
    name = cfg.milvus_collection
    if utility.has_collection(name):
        utility.drop_collection(name)
        print(f"dropped existing collection {name}", flush=True)
    await ensure_collection_async(cfg)

    total = await db.leads.count_documents({"embedding": True})
    print(f"{total} lead(s) marked embedding=True", flush=True)

    indexed = 0
    batch: list[dict] = []
    async for doc in db.leads.find({"embedding": True}):
        batch.append(doc)
        if len(batch) >= BATCH:
            indexed += await _flush(batch)
            batch = []
            print(f"indexed {indexed}/{total}", flush=True)
    indexed += await _flush(batch)

    print(f"DONE: {indexed} vector(s) upserted into {name}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
