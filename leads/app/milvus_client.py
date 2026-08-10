"""Milvus collection for lead embeddings keyed by Mongo _id.

Sync pymilvus calls run in a worker thread so they do not block the asyncio
event loop used by FastAPI request handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
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
from app.embeddings import embed_texts, lead_embedding_precedence, lead_embedding_texts

logger = logging.getLogger(__name__)

_ALIAS = "default"
_PK_MAX = 96
_MONGO_ID_MAX = 64
_APOLLO_ID_MAX = 128
_SHORT_MAX = 16

# UNIX-nice priority tiers for the Milvus gate: lower runs first. The pymilvus
# SDK is not reliably concurrent-safe, so every op still serializes through one
# gate — but waiters wake lowest-nice-first (FIFO within a nice), so an
# interactive similarity query jumps ahead of a wall of queued embedding writes
# instead of blocking behind them (FIFO would 90s-starve it → caller 502).
NICE_INTERACTIVE = 0  # search_similar (user query) — preempts the embed backlog
NICE_SEARCH_EMBED = 10  # embedding writes from a live search
NICE_BACKFILL = 20  # bulk / backfill embedding — yields to everything

# Bounded-skip fairness: a queued waiter may be passed over at most this many
# hand-offs before it is force-selected regardless of nice. Without this a
# sustained stream of NICE_INTERACTIVE acquisitions starves queued embed writers
# forever; with it, "reads usually win" still holds but a write is guaranteed to
# run within a bounded number of interactive ops.
_GATE_MAX_SKIPS = 8


@dataclass
class _GateWaiter:
    nice: int
    seq: int
    future: asyncio.Future[None]
    skips: int = 0


class MilvusGate:
    """Serializes Milvus SDK access, waking waiters lowest-``nice``-first.

    Same mutual exclusion as an ``asyncio.Lock`` (never two Milvus ops at once),
    but ordered: within a nice tier waiters are FIFO; across tiers a lower nice
    normally wins the next slot. Fairness is bounded — a waiter passed over
    ``max_skips`` times is force-selected regardless of nice, so a flood of
    interactive reads cannot starve embed writers indefinitely. Preemption is at
    op boundaries only — a running op is never interrupted. A waiter cancelled
    while queued removes itself cleanly (or, if it was already handed the slot,
    passes it on) so the gate never wedges.
    """

    def __init__(self, max_skips: int = _GATE_MAX_SKIPS) -> None:
        self._held = False
        self._waiters: list[_GateWaiter] = []
        self._seq = 0
        self._max_skips = max(1, int(max_skips))

    async def acquire(self, nice: int) -> None:
        if not self._held and not self._waiters:
            self._held = True
            return
        waiter = _GateWaiter(
            nice=nice,
            seq=self._seq,
            future=asyncio.get_running_loop().create_future(),
        )
        self._seq += 1
        self._waiters.append(waiter)
        try:
            await waiter.future
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # We were already handed the slot before the cancel landed; pass
                # it on so it is not lost (which would wedge the gate).
                self._wake_next()
            else:
                self._remove_waiter(waiter)
            raise
        self._held = True

    def release(self) -> None:
        self._wake_next()

    def _remove_waiter(self, waiter: _GateWaiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def _wake_next(self) -> None:
        # Drop any already-resolved waiters (e.g. cancelled while queued) so they
        # are never handed the slot.
        live = [w for w in self._waiters if not w.future.done()]
        if len(live) != len(self._waiters):
            self._waiters = live
        if not live:
            self._held = False
            return
        # Force-select the oldest waiter that has been skipped too many times;
        # otherwise the highest priority (lowest nice, then oldest seq).
        starved = [w for w in live if w.skips >= self._max_skips]
        if starved:
            chosen = min(starved, key=lambda w: w.seq)
        else:
            chosen = min(live, key=lambda w: (w.nice, w.seq))
        # Every waiter passed over this round moves one hand-off closer to its
        # skip ceiling, so no waiter can be deferred forever.
        for w in live:
            if w is not chosen:
                w.skips += 1
        self._remove_waiter(chosen)
        chosen.future.set_result(None)  # hand off; ``_held`` stays True

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
        # Primary key is per (doc, kind): a doc contributes up to three rows
        # (apollo / name / title) so the endpoint can average similarity across a
        # caller-selected subset of embed kinds.
        FieldSchema(
            name="pk",
            dtype=DataType.VARCHAR,
            is_primary=True,
            auto_id=False,
            max_length=_PK_MAX,
        ),
        FieldSchema(
            name="mongo_id",
            dtype=DataType.VARCHAR,
            max_length=_MONGO_ID_MAX,
        ),
        FieldSchema(
            name="apollo_id",
            dtype=DataType.VARCHAR,
            max_length=_APOLLO_ID_MAX,
        ),
        FieldSchema(name="entity_type", dtype=DataType.VARCHAR, max_length=_SHORT_MAX),
        FieldSchema(name="embed_kind", dtype=DataType.VARCHAR, max_length=_SHORT_MAX),
        # Derived top-level scalar filters ("" when absent for company_id).
        FieldSchema(name="company_id", dtype=DataType.VARCHAR, max_length=_APOLLO_ID_MAX),
        FieldSchema(name="has_email", dtype=DataType.BOOL),
        FieldSchema(name="has_phone", dtype=DataType.BOOL),
        FieldSchema(name="has_linkedin", dtype=DataType.BOOL),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    schema = CollectionSchema(
        fields=fields,
        description="Lead embeddings, one row per (doc, embed_kind); person and organization",
    )
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


@dataclass
class LeadVectorRow:
    """One Milvus row per (doc, embed_kind) in the v2 schema."""

    pk: str
    mongo_id: str
    apollo_id: str
    entity_type: str
    embed_kind: str
    company_id: str
    has_email: bool
    has_phone: bool
    has_linkedin: bool
    embedding: list[float]


def _upsert_lead_vectors_sync(
    rows: list[LeadVectorRow],
    *,
    settings: Settings | None = None,
) -> None:
    if not rows:
        return
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    # Column order must match the schema field order in ensure_collection.
    collection.upsert(
        [
            [row.pk[:_PK_MAX] for row in rows],
            [row.mongo_id[:_MONGO_ID_MAX] for row in rows],
            [row.apollo_id[:_APOLLO_ID_MAX] for row in rows],
            [row.entity_type[:_SHORT_MAX] for row in rows],
            [row.embed_kind[:_SHORT_MAX] for row in rows],
            [row.company_id[:_APOLLO_ID_MAX] for row in rows],
            [bool(row.has_email) for row in rows],
            [bool(row.has_phone) for row in rows],
            [bool(row.has_linkedin) for row in rows],
            [row.embedding for row in rows],
        ]
    )
    collection.flush()


async def upsert_lead_vectors(
    rows: list[LeadVectorRow],
    *,
    nice: int = NICE_SEARCH_EMBED,
    settings: Settings | None = None,
) -> None:
    """Upsert per-(doc, kind) lead vector rows into Milvus (thread offload).

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
    expr: str | None = None,
    limit: int = 10,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    if limit < 1:
        return []
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    search_kwargs: dict[str, Any] = dict(
        data=[query_vector],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=limit,
        output_fields=["mongo_id"],
    )
    if expr:
        search_kwargs["expr"] = expr
    results = collection.search(**search_kwargs)
    hits: list[tuple[str, float]] = []
    if not results:
        return hits
    for hit in results[0]:
        mongo_id = hit.entity.get("mongo_id") if hasattr(hit, "entity") else None
        if not mongo_id:
            continue
        score = float(hit.score)
        hits.append((str(mongo_id), score))
    return hits


async def search_similar(
    query_vector: list[float],
    *,
    expr: str | None = None,
    limit: int = 10,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    """Return [(mongo_id, score), ...] ordered by similarity (COSINE).

    ``expr`` is a Milvus boolean filter over the scalar fields (e.g.
    ``embed_kind == "apollo" and has_email == true``); the caller runs one search
    per embed kind and merges by ``mongo_id``.
    """
    # Interactive user query: jumps ahead of any queued embedding writes.
    async with _gate()(NICE_INTERACTIVE):
        return await asyncio.to_thread(
            _search_similar_sync,
            query_vector,
            expr=expr,
            limit=limit,
            settings=settings,
        )


def _milvus_str_literal(value: str) -> str:
    """Quote a string for a Milvus boolean expr (escape backslash and quote)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _query_apollo_scalars_sync(
    mongo_ids: list[str],
    *,
    settings: Settings | None = None,
) -> dict[str, dict[str, Any]]:
    if not mongo_ids:
        return {}
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    id_list = ", ".join(_milvus_str_literal(mid) for mid in mongo_ids)
    expr = f'mongo_id in [{id_list}] and embed_kind == "apollo"'
    rows = collection.query(
        expr=expr,
        output_fields=["mongo_id", "company_id", "has_email", "has_phone", "has_linkedin"],
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        mongo_id = str(row.get("mongo_id") or "")
        if mongo_id:
            out[mongo_id] = row
    return out


async def query_apollo_scalars(
    mongo_ids: list[str],
    *,
    nice: int = NICE_SEARCH_EMBED,
    settings: Settings | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{mongo_id: {company_id, has_email, has_phone, has_linkedin}}`` from the
    stored apollo-kind rows (a doc with no stored row is simply absent).

    Used to detect scalar drift on docs skipped by the never-downgrade precedence
    guard in ``index_lead_docs`` — a doc embedded at MATCH then enriched via BY_ID
    otherwise keeps stale ``has_email`` etc. in Milvus forever. Serializes on the
    gate like every pymilvus call.
    """
    if not mongo_ids:
        return {}
    async with _gate()(nice):
        return await asyncio.to_thread(
            _query_apollo_scalars_sync, mongo_ids, settings=settings
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

    Each doc contributes one row per embed kind (apollo / name / title) with the
    scalar fields taken from the doc's derived top-level fields. The precedence
    never-downgrade skip and the Mongo ``embedding`` flip key off the doc as
    before (the kinds ride along in the same upsert). Known acceptable staleness: a
    doc skipped by the precedence guard, or one that once had a title and later
    doesn't, may leave a stale/leftover Milvus row — harmless because query-time
    filters are re-checked authoritatively against Mongo (see the router).

    Soft-fails; returns ``[(mongo_id, stored_precedence), ...]`` (one per doc)
    for docs indexed.
    """
    cfg = settings or get_settings()
    if not docs:
        return []
    if not cfg.openai_configured:
        logger.warning("Skipping lead embedding: OPENAI_API_KEY not configured")
        return []

    @dataclass
    class _Prepared:
        mongo_id: str
        apollo_id: str
        entity_type: str
        company_id: str
        has_email: bool
        has_phone: bool
        has_linkedin: bool
        texts: dict[str, str]
        stored_precedence: int

    def _prepare(doc: dict[str, Any]) -> _Prepared | None:
        texts = lead_embedding_texts(doc)
        if not texts:
            return None
        # The vector reflects the best data available for this doc; the stored
        # precedence is never lowered (max over the doc's own tier + this source).
        stored_precedence = max(lead_embedding_precedence(doc), source_precedence)
        return _Prepared(
            mongo_id=str(doc.get("_id") or "").strip(),
            apollo_id=str(doc.get("apollo_id") or "").strip(),
            entity_type=doc.get("entity_type") or "person",
            company_id=str(doc.get("company_id") or ""),
            has_email=doc.get("email") is not None,
            has_phone=doc.get("phone") is not None,
            has_linkedin=doc.get("linkedin") is not None,
            texts=texts,
            stored_precedence=stored_precedence,
        )

    prepared: list[_Prepared] = []
    skipped_by_precedence: list[dict[str, Any]] = []
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
            # Defer for a scalar-drift check: the derived scalar fields (has_email,
            # …, company_id) may have advanced since this doc was embedded (e.g. a
            # MATCH-embedded doc later BY_ID-enriched), leaving Milvus stale.
            skipped_by_precedence.append(doc)
            continue
        item = _prepare(doc)
        if item is not None:
            prepared.append(item)

    # Re-index precedence-skipped docs ONLY when their current derived scalars differ
    # from the stored apollo-kind Milvus row. never-downgrade precedence is preserved
    # (``_prepare`` computes stored_precedence = max(doc-tier, source), which can't
    # lower the recorded tier). A scalar-query failure must never break ingest — log
    # and fall back to the plain skip.
    if skipped_by_precedence:
        drift_ids = [
            mongo_id
            for mongo_id in (str(doc.get("_id") or "").strip() for doc in skipped_by_precedence)
            if mongo_id
        ]
        try:
            stored_scalars = await query_apollo_scalars(drift_ids, nice=nice, settings=cfg)
        except Exception:
            logger.exception(
                "Scalar-drift check failed; leaving %s skipped doc(s) as-is", len(drift_ids)
            )
            stored_scalars = {}
        for doc in skipped_by_precedence:
            row = stored_scalars.get(str(doc.get("_id") or "").strip())
            if not row:
                continue
            current = (
                doc.get("email") is not None,
                doc.get("phone") is not None,
                doc.get("linkedin") is not None,
                str(doc.get("company_id") or ""),
            )
            stored = (
                bool(row.get("has_email")),
                bool(row.get("has_phone")),
                bool(row.get("has_linkedin")),
                str(row.get("company_id") or ""),
            )
            if current == stored:
                continue
            item = _prepare(doc)
            if item is not None:
                prepared.append(item)

    if not prepared:
        return []

    # Flatten to one embed input per (doc, kind), preserving alignment.
    flat: list[tuple[int, str]] = []
    all_texts: list[str] = []
    for doc_index, item in enumerate(prepared):
        for kind, text in item.texts.items():
            flat.append((doc_index, kind))
            all_texts.append(text)

    try:
        vectors = await embed_texts(all_texts, settings=cfg)
        rows: list[LeadVectorRow] = []
        for (doc_index, kind), vector in zip(flat, vectors, strict=True):
            item = prepared[doc_index]
            rows.append(
                LeadVectorRow(
                    pk=f"{item.mongo_id}:{kind}",
                    mongo_id=item.mongo_id,
                    apollo_id=item.apollo_id,
                    entity_type=item.entity_type,
                    embed_kind=kind,
                    company_id=item.company_id,
                    has_email=item.has_email,
                    has_phone=item.has_phone,
                    has_linkedin=item.has_linkedin,
                    embedding=vector,
                )
            )
        await upsert_lead_vectors(rows, nice=nice, settings=cfg)
        return [(item.mongo_id, item.stored_precedence) for item in prepared]
    except Exception:
        logger.exception("Failed to index %s lead(s) in Milvus", len(prepared))
        return []
