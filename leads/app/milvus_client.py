"""Milvus collection for lead embeddings keyed by Mongo _id.

Sync pymilvus calls run in a worker thread so they do not block the asyncio
event loop used by FastAPI request handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
from typing import Any
from urllib.parse import urlparse

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import Settings, get_settings
from app.embeddings import embed_texts, lead_embedding_precedence, lead_embedding_text

logger = logging.getLogger(__name__)

_ALIAS = "default"
_MONGO_ID_MAX = 64
_APOLLO_ID_MAX = 128

# UNIX-nice priority tiers for the Milvus gate: lower runs first. The pymilvus
# SDK is not reliably concurrent-safe, so every op still serializes through one
# gate — but waiters wake lowest-nice-first (FIFO within a nice), so an
# interactive similarity query jumps ahead of a wall of queued embedding writes
# instead of blocking behind them (FIFO would 90s-starve it → caller 502).
NICE_INTERACTIVE = 0  # search_similar (user query) — preempts the embed backlog
NICE_SEARCH_EMBED = 10  # embedding writes from a live search
NICE_BACKFILL = 20  # bulk / backfill embedding — yields to everything


class MilvusGate:
    """Serializes Milvus SDK access, waking waiters lowest-``nice``-first.

    Same mutual exclusion as an ``asyncio.Lock`` (never two Milvus ops at once),
    but ordered: within a nice tier waiters are FIFO; across tiers a lower nice
    always wins the next slot. Preemption is at op boundaries only — a running op
    is never interrupted. A waiter cancelled while queued removes itself cleanly
    (or, if it was already handed the slot, passes it on) so the gate never wedges.
    """

    def __init__(self) -> None:
        self._held = False
        # Min-heap of (nice, seq, future); seq breaks ties FIFO within a nice.
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = 0

    async def acquire(self, nice: int) -> None:
        if not self._held and not self._waiters:
            self._held = True
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (nice, self._seq, fut))
        self._seq += 1
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # We were already handed the slot before the cancel landed; pass
                # it on so it is not lost (which would wedge the gate).
                self._wake_next()
            else:
                self._waiters = [w for w in self._waiters if w[2] is not fut]
                heapq.heapify(self._waiters)
            raise
        self._held = True

    def release(self) -> None:
        self._wake_next()

    def _wake_next(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)  # hand off; ``_held`` stays True
                return
        self._held = False

    @contextlib.asynccontextmanager
    async def __call__(self, nice: int = NICE_INTERACTIVE):
        await self.acquire(nice)
        try:
            yield
        finally:
            self.release()


# One gate per event loop (lazy-init, mirroring the prior lock pattern) so it is
# bound to the running loop and never shared across loops in tests.
_milvus_gate: MilvusGate | None = None
_milvus_gate_loop: asyncio.AbstractEventLoop | None = None


def _gate() -> MilvusGate:
    global _milvus_gate, _milvus_gate_loop
    loop = asyncio.get_running_loop()
    if _milvus_gate is None or _milvus_gate_loop is not loop:
        _milvus_gate = MilvusGate()
        _milvus_gate_loop = loop
    return _milvus_gate


def _parse_uri(uri: str) -> tuple[str, int]:
    raw = uri.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or "milvus"
    port = parsed.port or 19530
    return host, port


def connect_milvus(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    host, port = _parse_uri(cfg.milvus_uri)
    if connections.has_connection(_ALIAS):
        try:
            connections.get_connection_addr(_ALIAS)
            return
        except Exception:
            connections.disconnect(_ALIAS)
    connections.connect(alias=_ALIAS, host=host, port=str(port))


def close_milvus() -> None:
    if connections.has_connection(_ALIAS):
        connections.disconnect(_ALIAS)


def ensure_collection(settings: Settings | None = None) -> Collection:
    cfg = settings or get_settings()
    connect_milvus(cfg)
    name = cfg.milvus_collection
    dim = cfg.openai_embedding_dimensions

    if utility.has_collection(name):
        collection = Collection(name)
        collection.load()
        return collection

    fields = [
        FieldSchema(
            name="mongo_id",
            dtype=DataType.VARCHAR,
            is_primary=True,
            auto_id=False,
            max_length=_MONGO_ID_MAX,
        ),
        FieldSchema(
            name="apollo_id",
            dtype=DataType.VARCHAR,
            max_length=_APOLLO_ID_MAX,
        ),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    schema = CollectionSchema(fields=fields, description="Lead embeddings (person and organization)")
    collection = Collection(name=name, schema=schema)
    collection.create_index(
        field_name="embedding",
        index_params={
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
            "params": {"nlist": 128},
        },
    )
    collection.load()
    logger.info("Created Milvus collection %s (dim=%s)", name, dim)
    return collection


async def ensure_collection_async(settings: Settings | None = None) -> Collection:
    async with _gate()(NICE_INTERACTIVE):
        return await asyncio.to_thread(ensure_collection, settings)


def _upsert_lead_vectors_sync(
    rows: list[tuple[str, str, list[float]]],
    *,
    settings: Settings | None = None,
) -> None:
    if not rows:
        return
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    mongo_ids = [mongo_id for mongo_id, _, _ in rows]
    apollo_ids = [apollo_id[:_APOLLO_ID_MAX] for _, apollo_id, _ in rows]
    vectors = [vector for _, _, vector in rows]
    collection.upsert([mongo_ids, apollo_ids, vectors])
    collection.flush()


async def upsert_lead_vectors(
    rows: list[tuple[str, str, list[float]]],
    *,
    nice: int = NICE_SEARCH_EMBED,
    settings: Settings | None = None,
) -> None:
    """Upsert (mongo_id, apollo_id, embedding) rows into Milvus (thread offload).

    ``nice`` sets the gate priority of this write: live-search embeds pass
    ``NICE_SEARCH_EMBED`` and backfills ``NICE_BACKFILL`` so an interactive
    ``search_similar`` (``NICE_INTERACTIVE``) always wins the next gate slot.
    """
    if not rows:
        return
    async with _gate()(nice):
        await asyncio.to_thread(_upsert_lead_vectors_sync, rows, settings=settings)


def _search_similar_sync(
    query_vector: list[float],
    *,
    limit: int = 10,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    if limit < 1:
        return []
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    results = collection.search(
        data=[query_vector],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=limit,
        output_fields=["mongo_id", "apollo_id"],
    )
    hits: list[tuple[str, float]] = []
    if not results:
        return hits
    for hit in results[0]:
        mongo_id = hit.entity.get("mongo_id") if hasattr(hit, "entity") else None
        if mongo_id is None:
            mongo_id = getattr(hit, "id", None)
        if not mongo_id:
            continue
        score = float(hit.score)
        hits.append((str(mongo_id), score))
    return hits


async def search_similar(
    query_vector: list[float],
    *,
    limit: int = 10,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    """Return [(mongo_id, score), ...] ordered by similarity (COSINE)."""
    # Interactive user query: jumps ahead of any queued embedding writes.
    async with _gate()(NICE_INTERACTIVE):
        return await asyncio.to_thread(
            _search_similar_sync,
            query_vector,
            limit=limit,
            settings=settings,
        )


async def index_lead_docs(
    docs: list[dict[str, Any]],
    *,
    source_precedence: int,
    force: bool = False,
    nice: int = NICE_SEARCH_EMBED,
    settings: Settings | None = None,
) -> list[tuple[str, int]]:
    """Embed and upsert person/organization Mongo docs, respecting embedding precedence.

    ``source_precedence`` is the precedence of the operation requesting the embed
    (search < complete-info < match). A doc is skipped when its existing Milvus
    vector was produced from a strictly higher precedence source, so e.g. a broad
    search never overwrites a match/complete-info embedding.

    ``force`` bypasses that never-downgrade skip so a backfill can re-embed docs
    that already carry a vector (e.g. after an embedding-model/text change); the
    stored precedence still reflects the doc's own best data tier, never lowered.

    ``nice`` is the Milvus-gate priority of the resulting upsert (default
    ``NICE_SEARCH_EMBED``; backfills pass ``NICE_BACKFILL``). Only the short Milvus
    upsert serializes on the gate — the slower OpenAI embed runs lock-free.

    Soft-fails; returns ``[(mongo_id, stored_precedence), ...]`` for docs indexed.
    """
    cfg = settings or get_settings()
    if not docs:
        return []
    if not cfg.openai_configured:
        logger.warning("Skipping lead embedding: OPENAI_API_KEY not configured")
        return []

    prepared: list[tuple[str, str, str, int]] = []
    for doc in docs:
        entity_type = doc.get("entity_type") or "person"
        if entity_type not in ("person", "organization"):
            continue
        mongo_id = str(doc.get("_id") or "").strip()
        apollo_id = str(doc.get("apollo_id") or "").strip()
        if not mongo_id or not apollo_id:
            continue
        existing_precedence = int(doc.get("embedding_source") or 0)
        # Never downgrade: skip if a higher-precedence vector is already stored
        # (unless force — a re-embed must rewrite every doc regardless of precedence,
        # e.g. after an embedding-model or embedding-text change).
        if not force and existing_precedence and source_precedence < existing_precedence:
            continue
        text = lead_embedding_text(doc)
        if not text.strip():
            continue
        # The vector reflects the best data available for this doc.
        stored_precedence = max(lead_embedding_precedence(doc), source_precedence)
        prepared.append((mongo_id, apollo_id, text, stored_precedence))

    if not prepared:
        return []

    try:
        vectors = await embed_texts([text for _, _, text, _ in prepared], settings=cfg)
        rows = [
            (mongo_id, apollo_id, vector)
            for (mongo_id, apollo_id, _, _), vector in zip(prepared, vectors, strict=True)
        ]
        await upsert_lead_vectors(rows, nice=nice, settings=cfg)
        return [(mongo_id, precedence) for (mongo_id, _, _, precedence) in prepared]
    except Exception:
        logger.exception("Failed to index %s lead(s) in Milvus", len(prepared))
        return []
