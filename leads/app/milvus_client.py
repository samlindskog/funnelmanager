"""Milvus collection for lead embeddings keyed by Mongo _id.

Sync pymilvus calls run in a worker thread so they do not block the asyncio
event loop used by FastAPI request handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
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

# Live-ingest flush pacing (see _upsert_lead_vectors_sync).
_FLUSH_EVERY_ROWS = 10_000
_rows_since_flush = 0

# Milvus write-pressure cooperation: the prod cluster runs quotaAndLimits
# growing-segment + memory protection, so an upsert under pressure raises a
# rate-limit / quota-deny error instead of succeeding. Treat that class as
# retryable with bounded exponential backoff (the gate is released between
# attempts so interactive similarity queries still run). After the total budget
# is spent the error propagates so the caller records a real failure.
_UPSERT_MAX_BACKOFF_TOTAL_SECONDS = 60.0
_UPSERT_MAX_SINGLE_BACKOFF_SECONDS = 16.0
_WRITE_PRESSURE_MARKERS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "quota",
    "deny to write",
    "force deny",
    "memory protection",
    "disk protection",
    "disk quota",
    "too many",
)


def _is_milvus_write_pressure(exc: BaseException) -> bool:
    """True when ``exc`` looks like Milvus throttling/denying a write under pressure.

    Matches pymilvus rate-limit / quota-deny messages by substring (the SDK does
    not give a stable dedicated exception type across these). Conservative: an
    unrecognized error is treated as a hard failure, not retried.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _WRITE_PRESSURE_MARKERS)


# Connection/availability markers, so a Milvus-down error is classified distinctly
# from a generic embed failure (for the sanitized event code — never leak raw text).
_UNAVAILABLE_MARKERS = (
    "connect",
    "connection",
    "unavailable",
    "timed out",
    "timeout",
    "refused",
    "unreachable",
)


def classify_write_error(exc: BaseException) -> str:
    """Classify a hard embed/upsert failure into a FIXED, non-sensitive code.

    Returned verbatim to browsers via the stream ``item_error``/``error`` events, so
    it must NEVER carry Milvus URIs / quota internals / SDK exception text — those
    are logged server-side only. One of: ``milvus_write_pressure``,
    ``milvus_unavailable``, ``embedding_failed``.
    """
    if _is_milvus_write_pressure(exc):
        return "milvus_write_pressure"
    text = str(exc).lower()
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        return "milvus_unavailable"
    return "embedding_failed"


@dataclass
class WritePressureBudget:
    """Amortized write-pressure retry budget shared across one embedding stream.

    All chunks of a stream share ONE budget (bound to the ``upsert_lead_vectors``
    calls via ``use_write_pressure_budget``), so a big stream under sustained Milvus
    pressure spends a single bounded total (config
    ``embed_write_pressure_budget_seconds``) rather than a fresh per-chunk budget
    that would multiply into hours. Once spent, pressure-failures propagate at once.
    """

    remaining: float

    @classmethod
    def create(cls, total_seconds: float) -> "WritePressureBudget":
        return cls(remaining=max(0.0, float(total_seconds)))


# Bound to the current embedding stream's budget for the duration of an
# ``embed_batch`` call (set by ``schedule_embedding_batch``); ``None`` for one-off
# enrich/match/background embeds, which fall back to the per-call cap below.
_write_pressure_budget: contextvars.ContextVar[WritePressureBudget | None] = (
    contextvars.ContextVar("milvus_write_pressure_budget", default=None)
)


@contextlib.contextmanager
def use_write_pressure_budget(budget: "WritePressureBudget | None"):
    """Bind ``budget`` as the active per-stream write-pressure budget within scope."""
    token = _write_pressure_budget.set(budget)
    try:
        yield
    finally:
        _write_pressure_budget.reset(token)


class LeadIndexingError(Exception):
    """A hard embed/Milvus failure for a batch, carrying honest attempt accounting.

    ``attempted`` = docs that were actually going to be upserted (already past the
    never-downgrade precedence skip), so the streamed caller charges only these as
    failed and counts precedence-skips as done-but-not-failed. ``code`` is the
    sanitized classification (see ``classify_write_error``).
    """

    def __init__(self, *, attempted: int, code: str) -> None:
        super().__init__(code)
        self.attempted = attempted
        self.code = code

# UNIX-nice priority tiers for the Milvus gate: lower runs first. The pymilvus
# SDK is not reliably concurrent-safe, so every op still serializes through one
# gate — but waiters wake lowest-nice-first (FIFO within a nice), so an
# interactive similarity query jumps ahead of a wall of queued embedding writes
# instead of blocking behind them (FIFO would 90s-starve it → caller 502).
NICE_INTERACTIVE = 0  # search_similar (a lone user query) — preempts everything
NICE_BULK_READ = 5  # a fan-out of many reads (grouped per-company search): yields
# to a lone interactive query so one human still wins the gate, but still preempts
# embedding writes — it is a read the caller is blocked on, not a background write.
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
            # Index-level mmap: IVF_FLAT keeps full vectors in the index, so
            # collection-level mmap alone leaves ~GBs anonymous-RAM-resident.
            # Applied live 2026-08-13 (working set 7.8Gi -> 1.0Gi); set here so
            # rebuilds inherit it.
            "mmap.enabled": "true",
        },
    )
    # mmap the collection: sealed data loads via the page cache instead of
    # resident RAM, so memory stops scaling linearly with corpus growth (the
    # corpus grows on every search; 2.5Gi->4Gi->6Gi->8Gi OOM ladder, 2026-08-1x).
    # Slight query-latency cost, acceptable for this workload. Set at creation
    # so migration rebuilds (reembed.py drop/recreate) inherit it.
    collection.set_properties({"mmap.enabled": "true"})
    collection.load()
    logger.info("Created Milvus collection %s (dim=%s, mmap on)", name, dim)
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
    # float32 np.ndarray (pymilvus 2.5.4 takes it directly for FLOAT_VECTOR);
    # a plain list[float] is still accepted for the defensive embed fallback.
    embedding: Any


@dataclass
class _Prepared:
    """A lead doc resolved to its per-kind embed texts + scalar fields, ready to embed."""

    mongo_id: str
    apollo_id: str
    entity_type: str
    company_id: str
    has_email: bool
    has_phone: bool
    has_linkedin: bool
    texts: dict[str, str]
    stored_precedence: int


def _prepare_lead_row(doc: dict[str, Any], source_precedence: int) -> _Prepared | None:
    """Resolve a Mongo lead doc to a ``_Prepared`` (None when it has no embed text).

    The stored precedence is never lowered: it is ``max`` over the doc's own data
    tier (``lead_embedding_precedence``) and the requesting ``source_precedence``.
    """
    texts = lead_embedding_texts(doc)
    if not texts:
        return None
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
    # Periodic (never per-batch) flush, mirroring scripts/reembed.py's pacing:
    # per-batch flushing seals a tiny segment per upsert (segment storm, 4-5x
    # slowdown, 2026-08-10); NEVER flushing lets unsealed growing segments pile
    # up during sustained bulk ingest until Milvus OOMs — and then OOMs again
    # replaying their WAL at startup (2026-08-12 Prospect-run outage; mmap only
    # covers sealed data). ~10k rows is the proven balance. Safe here because
    # every call is serialized by the MilvusGate.
    global _rows_since_flush
    _rows_since_flush += len(rows)
    if _rows_since_flush >= _FLUSH_EVERY_ROWS:
        collection.flush()
        _rows_since_flush = 0


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

    Under Milvus write-pressure (rate-limit / quota-deny) the upsert is retried
    with bounded exponential backoff. The gate is released between attempts so the
    backoff does not block interactive queries; embedding naturally stalls, which —
    via the bounded ingest queue upstream — pauses the Apollo walk.

    The backoff budget is amortized PER STREAM: if a ``WritePressureBudget`` is bound
    (``use_write_pressure_budget``, set by the streamed embed path) every chunk of
    that stream draws down the same shared total, so a big stream can't retry for
    hours. Unbound (one-off enrich/match/background embed) it falls back to a bounded
    per-call cap. Once the budget is spent the error propagates so the caller records
    the chunk as failed.
    """
    if not rows:
        return
    budget = _write_pressure_budget.get()
    attempt = 0
    call_waited = 0.0
    while True:
        try:
            async with _gate()(nice):
                await asyncio.to_thread(_upsert_lead_vectors_sync, rows, settings=settings)
            return
        except Exception as exc:
            if not _is_milvus_write_pressure(exc):
                raise
            # Remaining budget: shared per-stream if bound, else the per-call cap.
            if budget is not None:
                remaining = budget.remaining
            else:
                remaining = _UPSERT_MAX_BACKOFF_TOTAL_SECONDS - call_waited
            if remaining <= 0:
                raise
            backoff = min(2.0**attempt, _UPSERT_MAX_SINGLE_BACKOFF_SECONDS, remaining)
            logger.warning(
                "Milvus write-pressure on upsert (%s); backing off %.1fs (remaining budget %.1fs)",
                exc,
                backoff,
                remaining,
            )
            # Gate released here (outside the async with) so interactive queries run.
            await asyncio.sleep(backoff)
            if budget is not None:
                budget.remaining = max(0.0, budget.remaining - backoff)
            else:
                call_waited += backoff
            attempt += 1


def _search_similar_sync(
    query_vector: Any,
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
    query_vector: Any,
    *,
    expr: str | None = None,
    limit: int = 10,
    nice: int = NICE_INTERACTIVE,
    settings: Settings | None = None,
) -> list[tuple[str, float]]:
    """Return [(mongo_id, score), ...] ordered by similarity (COSINE).

    ``expr`` is a Milvus boolean filter over the scalar fields (e.g.
    ``embed_kind == "apollo" and has_email == true``); the caller runs one search
    per embed kind and merges by ``mongo_id``.

    ``nice`` sets the gate priority: a lone user query keeps the default
    ``NICE_INTERACTIVE`` (jumps ahead of any queued embedding writes); a large
    read fan-out (grouped per-company search) passes ``NICE_BULK_READ`` so one
    interactive query still outranks the whole fan-out.
    """
    async with _gate()(nice):
        return await asyncio.to_thread(
            _search_similar_sync,
            query_vector,
            expr=expr,
            limit=limit,
            settings=settings,
        )


# Milvus grouping-search "feature unsupported / param rejected" markers. Distinct
# from generic failures (connection/quota) so the caller can fall back to the
# per-company fan-out ONLY for a real grouping-capability gap, not any error.
_GROUPING_UNSUPPORTED_MARKERS = (
    "group_by",
    "group by",
    "groupby",
    "group_size",
    "group size",
    "groupsize",
    "grouping",
)


def is_grouping_unsupported(exc: BaseException) -> bool:
    """True when ``exc`` is a grouping-search capability/param rejection.

    Matches only grouping-specific tokens, so a connection/quota error (no such
    token) is NOT misread as "unsupported" and still surfaces as a real failure.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _GROUPING_UNSUPPORTED_MARKERS)


def _search_similar_grouped_sync(
    query_vector: Any,
    *,
    expr: str | None,
    group_by_field: str,
    group_size: int,
    limit: int,
    settings: Settings | None = None,
) -> list[tuple[str, str, float]]:
    if limit < 1:
        return []
    cfg = settings or get_settings()
    collection = ensure_collection(cfg)
    search_kwargs: dict[str, Any] = dict(
        data=[query_vector],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=limit,
        output_fields=["mongo_id", "company_id"],
        # Grouping search: return up to ``group_size`` hits per distinct
        # ``group_by_field`` value (company), across ``limit`` groups.
        # strict_group_size=False => a group with fewer docs returns fewer (not
        # padded), which is what we want.
        group_by_field=group_by_field,
        group_size=group_size,
        strict_group_size=False,
    )
    if expr:
        search_kwargs["expr"] = expr
    results = collection.search(**search_kwargs)
    hits: list[tuple[str, str, float]] = []
    if not results:
        return hits
    for hit in results[0]:
        entity = hit.entity if hasattr(hit, "entity") else None
        mongo_id = entity.get("mongo_id") if entity else None
        if not mongo_id:
            continue
        company_id = (entity.get("company_id") if entity else None) or ""
        hits.append((str(mongo_id), str(company_id), float(hit.score)))
    return hits


async def search_similar_grouped(
    query_vector: Any,
    *,
    expr: str | None = None,
    group_by_field: str = "company_id",
    group_size: int = 10,
    limit: int = 10,
    nice: int = NICE_BULK_READ,
    settings: Settings | None = None,
) -> list[tuple[str, str, float]]:
    """Grouping ANN search: [(mongo_id, company_id, score), ...] across groups.

    Returns up to ``group_size`` hits per distinct ``group_by_field`` value over
    ``limit`` groups (one Milvus call replaces a per-group fan-out). ``nice``
    defaults to ``NICE_BULK_READ`` (this is a bulk read). May raise a
    grouping-unsupported error (see ``is_grouping_unsupported``) which the caller
    treats as a fallback trigger rather than a hard failure.
    """
    async with _gate()(nice):
        return await asyncio.to_thread(
            _search_similar_grouped_sync,
            query_vector,
            expr=expr,
            group_by_field=group_by_field,
            group_size=group_size,
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


async def _reindex_scalar_drift(
    skipped_by_precedence: list[dict[str, Any]],
    *,
    source_precedence: int,
    nice: int,
    settings: Settings,
) -> list[_Prepared]:
    """Re-prepare precedence-skipped docs whose derived scalars drifted from Milvus.

    ``index_lead_docs`` skips docs whose stored vector came from a strictly higher
    precedence source. But the derived scalar fields (``has_email`` …,
    ``company_id``) may have advanced since (e.g. a MATCH-embedded doc later
    BY_ID-enriched), leaving the stored Milvus row stale. For each skipped doc
    whose current scalars differ from its stored apollo-kind row, return a
    ``_Prepared`` so it is re-indexed. never-downgrade precedence is preserved
    (``_prepare_lead_row`` takes ``max(doc-tier, source)``). A scalar-query failure
    must never break ingest — it is logged and every skipped doc is left as-is.
    """
    if not skipped_by_precedence:
        return []
    drift_ids = [
        mongo_id
        for mongo_id in (str(doc.get("_id") or "").strip() for doc in skipped_by_precedence)
        if mongo_id
    ]
    try:
        stored_scalars = await query_apollo_scalars(drift_ids, nice=nice, settings=settings)
    except Exception:
        logger.exception(
            "Scalar-drift check failed; leaving %s skipped doc(s) as-is", len(drift_ids)
        )
        return []
    out: list[_Prepared] = []
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
        item = _prepare_lead_row(doc, source_precedence)
        if item is not None:
            out.append(item)
    return out


async def index_lead_docs(
    docs: list[dict[str, Any]],
    *,
    source_precedence: int,
    force: bool = False,
    nice: int = NICE_SEARCH_EMBED,
    raise_on_failure: bool = False,
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

    Returns ``[(mongo_id, stored_precedence), ...]`` (one per doc) for docs indexed
    — fewer than ``docs`` when some are skipped by never-downgrade precedence.

    ``raise_on_failure`` controls what happens on a HARD embed/Milvus failure
    (after write-pressure retries are exhausted): the default ``False`` soft-fails
    (logs + returns ``[]``, preserving the single-enrich/match/backfill behavior of
    saving the Mongo doc and leaving the vector for a later backfill); the streamed
    embed path passes ``True`` so the failure propagates and the stream can record
    the chunk as failed (honest progress) instead of claiming 100%.
    """
    cfg = settings or get_settings()
    if not docs:
        return []
    if not cfg.openai_configured:
        logger.warning("Skipping lead embedding: OPENAI_API_KEY not configured")
        return []

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
        item = _prepare_lead_row(doc, source_precedence)
        if item is not None:
            prepared.append(item)

    # Re-index precedence-skipped docs whose current derived scalars have drifted
    # from their stored Milvus row (helper preserves never-downgrade precedence).
    prepared.extend(
        await _reindex_scalar_drift(
            skipped_by_precedence,
            source_precedence=source_precedence,
            nice=nice,
            settings=cfg,
        )
    )

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
    except Exception as exc:
        logger.exception("Failed to index %s lead(s) in Milvus", len(prepared))
        if raise_on_failure:
            # Carry the ATTEMPTED count (docs past the precedence skip) + a sanitized
            # code so the streamed caller charges only real attempts as failed and
            # never relays raw exception text to browsers.
            raise LeadIndexingError(
                attempted=len(prepared), code=classify_write_error(exc)
            ) from exc
        return []
