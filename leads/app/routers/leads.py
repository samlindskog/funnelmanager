import asyncio
import logging
import re
import secrets
from collections.abc import AsyncIterator, Collection
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote

import orjson
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fm_runtime import anonymous, confirmation_threshold, require_confirmation
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.apollo import ApolloLeadsClient
from app.apollo_endpoints import (
    ORG_BY_ID,
    ORG_DISPLAY_PRIORITY,
    ORG_SEARCH,
    PERSON_BY_ID,
    PERSON_DISPLAY_PRIORITY,
    PERSON_MATCH,
    PERSON_SEARCH,
    empty_enriched_flags,
    endpoint_entry,
    merge_enriched_flags,
    normalize_apollo_enriched,
    responses_from_doc,
)
from app.config import Settings, get_settings
from app.database import get_database, get_db
from app.derived import derive_top_fields
from app.embeddings import (
    EMBED_SOURCE_COMPLETE_INFO,
    EMBED_SOURCE_MATCH,
    EMBED_SOURCE_SEARCH,
    embed_texts,
    endpoint_source_precedence,
)
from app.milvus_client import (
    NICE_BACKFILL,
    NICE_BULK_READ,
    NICE_INTERACTIVE,
    NICE_SEARCH_EMBED,
    _milvus_str_literal,
    classify_write_error,
    index_lead_docs,
    is_grouping_unsupported,
    search_similar,
    search_similar_grouped,
)
from app.schemas import (
    BatchExistsRequest,
    ApolloEndpointResponseOut,
    ApolloEnrichedFlags,
    ApolloParamsBody,
    BatchMongoIdsRequest,
    EmbeddingBackfillResponse,
    GroupedSimilaritySearchRequest,
    GroupedSimilaritySearchResponse,
    LeadOut,
    SearchIdsOut,
    SimilarityGroupOut,
    SimilarityHitOut,
    SimilaritySearchRequest,
    SimilaritySearchResponse,
    StreamControlResponse,
    StreamSubscribeRequest,
)
from app.stream_jobs import (
    _safe_ingest_error_detail,
    cancel_stream,
    close_embedding_stream,
    create_embedding_stream,
    iter_stream_events,
    run_paged_search_with_embedding,
    schedule_embedding_batch,
    set_stream_paused,
    stream_job_manager,
    stream_status,
)

logger = logging.getLogger(__name__)

# Authorization is enforced by the mesh (Istio + OPA ext_authz) and by
# fm_runtime's PrincipalMiddleware (401 without a leads-audience JWT) — the
# only routes reachable without a principal are the ones annotated @anonymous
# below (Apollo webhooks: secret-in-path, and the legacy health probe).
router = APIRouter(prefix="/api/leads", tags=["leads"])


def _serialize_responses(responses: dict[str, Any]) -> dict[str, ApolloEndpointResponseOut]:
    out: dict[str, ApolloEndpointResponseOut] = {}
    for key, entry in responses.items():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        received = entry.get("received_at")
        if not isinstance(received, datetime):
            try:
                received = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
            except Exception:
                received = datetime.now(timezone.utc)
        out[str(key)] = ApolloEndpointResponseOut(received_at=received, data=data)
    return out


def _display_endpoint_key(
    responses: dict[str, Any], priority: tuple[str, ...]
) -> str | None:
    """Key of the highest-precedence stored response carrying data (mirrors
    ``entity_payload_from_responses`` selection, but returns the key)."""
    for key in priority:
        entry = responses.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict) and entry.get("data"):
            return key
    for key, entry in responses.items():
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict) and entry.get("data"):
            return key
    return None


def _select_display_responses(
    responses: dict[str, Any], entity_type: str
) -> dict[str, Any]:
    """Slim ``apollo_responses`` to the display subset (see ``BatchMongoIdsRequest.fields``).

    People keep the display payload plus the search and match entries (the search
    UI's contact resolvers read those two directly); organizations keep only the
    display payload. Empty/malformed entries are dropped downstream by
    ``_serialize_responses``.
    """
    if entity_type == "organization":
        priority = ORG_DISPLAY_PRIORITY
        # Keep the search entry alongside the display payload (mirrors people):
        # dropping it lost user-visible raw-response data in the org detail pane.
        always_keep: tuple[str, ...] = (ORG_SEARCH,)
    else:
        priority = PERSON_DISPLAY_PRIORITY
        always_keep = (PERSON_SEARCH, PERSON_MATCH)
    keep: set[str] = set()
    display_key = _display_endpoint_key(responses, priority)
    if display_key is not None:
        keep.add(display_key)
    keep.update(key for key in always_keep if key in responses)
    return {key: entry for key, entry in responses.items() if key in keep}


def _serialize_lead(doc: dict[str, Any], fields: str = "full") -> LeadOut:
    responses = responses_from_doc(doc)
    # Enrichment flags always reflect the full stored responses — display mode
    # slims only the emitted apollo_responses, never the derived/flag fields.
    flags = normalize_apollo_enriched(doc, responses=responses)
    entity_type = doc.get("entity_type") or "person"
    out_responses = (
        _select_display_responses(responses, entity_type)
        if fields == "display"
        else responses
    )
    return LeadOut(
        id=str(doc["_id"]),
        apollo_id=str(doc["apollo_id"]),
        entity_type=entity_type,
        embedding=bool(doc.get("embedding")),
        apollo_enriched=ApolloEnrichedFlags(**flags),
        apollo_responses=_serialize_responses(out_responses),
        name=doc.get("name"),
        title=doc.get("title"),
        company_id=doc.get("company_id"),
        company_apollo_id=doc.get("company_apollo_id"),
        email=doc.get("email"),
        phone=doc.get("phone"),
        linkedin=doc.get("linkedin"),
        derived_at=doc.get("derived_at"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


async def _mark_embedded(indexed: list[tuple[str, int]]) -> None:
    """Record ``embedding=True`` and the vector's source precedence per indexed lead."""
    if not indexed:
        return
    now = datetime.now(timezone.utc)
    db = get_db()
    by_precedence: dict[int, list[ObjectId]] = {}
    for raw_id, precedence in indexed:
        try:
            object_id = ObjectId(str(raw_id))
        except Exception:
            continue
        by_precedence.setdefault(int(precedence), []).append(object_id)
    for precedence, object_ids in by_precedence.items():
        if not object_ids:
            continue
        await db.leads.update_many(
            {"_id": {"$in": object_ids}},
            {
                "$set": {
                    "embedding": True,
                    "embedding_source": precedence,
                    "updated_at": now,
                }
            },
        )


async def _embed_mongo_ids_batch(
    mongo_ids: list[str],
    *,
    source_precedence: int,
    force: bool = False,
    nice: int = NICE_SEARCH_EMBED,
    raise_on_failure: bool = True,
) -> list[str]:
    """Embed leads (respecting precedence) and record source. Returns indexed mongo ids.

    ``force`` re-embeds even docs that already carry a vector (backfill); see
    ``index_lead_docs``. ``nice`` sets the Milvus-gate priority of the upsert
    (live search passes ``NICE_SEARCH_EMBED``; backfill passes ``NICE_BACKFILL``).

    ``raise_on_failure`` defaults ``True``: this is the streamed/background embed
    path, so a hard Milvus failure propagates to the caller (the stream records the
    chunk as failed — honest progress — and the background embed logs it) rather
    than being silently swallowed to ``[]``.
    """
    if not mongo_ids:
        return []
    db = get_db()
    object_ids: list[ObjectId] = []
    for raw in mongo_ids:
        try:
            object_ids.append(ObjectId(str(raw)))
        except Exception:
            continue
    if not object_ids:
        return []
    docs = [doc async for doc in db.leads.find({"_id": {"$in": object_ids}})]
    indexed = await index_lead_docs(
        docs,
        source_precedence=source_precedence,
        force=force,
        nice=nice,
        raise_on_failure=raise_on_failure,
    )
    if indexed:
        await _mark_embedded(indexed)
    return [mongo_id for mongo_id, _ in indexed]


async def _background_embed_mongo_ids(mongo_ids: list[str], source_precedence: int) -> None:
    """Embed leads in the background (no stream progress).

    Bulk, non-interactive work — runs at ``NICE_BACKFILL`` so it yields the Milvus
    gate to live-search embeds and interactive similarity queries.
    """
    if not mongo_ids:
        return
    try:
        await _embed_mongo_ids_batch(
            mongo_ids, source_precedence=source_precedence, nice=NICE_BACKFILL
        )
    except Exception:
        logger.exception("Background embedding failed for %s lead(s)", len(mongo_ids))


def _schedule_background_embed(
    background_tasks: BackgroundTasks | None,
    mongo_ids: list[str],
    *,
    source_precedence: int,
) -> None:
    if not mongo_ids:
        return
    unique_ids = list(dict.fromkeys(mongo_ids))
    if background_tasks is not None:
        background_tasks.add_task(_background_embed_mongo_ids, unique_ids, source_precedence)
    else:
        asyncio.create_task(_background_embed_mongo_ids(unique_ids, source_precedence))


# Never forward client-supplied Apollo credentials; the server uses APOLLO_API_KEY from env.
_APOLLO_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "apollo_api_key",
        "apollo_key",
        "x_api_key",
        "x-api-key",
        "authorization",
        "bearer",
    }
)


# Local control flags — never forwarded to Apollo.
_APOLLO_CONTROL_KEYS = frozenset({"stream"})


def _params_dict(body: ApolloParamsBody | None) -> dict[str, Any]:
    if body is None:
        return {}
    return {
        key: value
        for key, value in body.model_dump(exclude_none=True).items()
        if key.lower() not in _APOLLO_CREDENTIAL_KEYS
        and key.lower() not in _APOLLO_CONTROL_KEYS
    }


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _first_param_id(params: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = params.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _person_id_from_record(record: dict[str, Any]) -> str | None:
    value = record.get("id")
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _organization_id_from_record(record: dict[str, Any]) -> str | None:
    for key in ("organization_id", "id"):
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _person_from_complete_response(apollo_response: dict[str, Any]) -> dict[str, Any]:
    person = apollo_response.get("person")
    if isinstance(person, dict):
        return person
    if apollo_response.get("id"):
        return apollo_response
    return {}


def _organization_from_complete_response(apollo_response: dict[str, Any]) -> dict[str, Any]:
    for key in ("organization", "account"):
        value = apollo_response.get(key)
        if isinstance(value, dict):
            return value
    if apollo_response.get("id"):
        return apollo_response
    return {}


def _embedding_flag_after_update(existing: dict[str, Any], endpoint: str) -> bool:
    """Keep ``embedding=True`` when the stored vector outranks this endpoint.

    ``index_lead_docs`` skips docs whose vector came from a strictly higher
    precedence source; resetting the flag for those would leave it False forever
    even though the (better) vector still exists in Milvus.
    """
    if not existing.get("embedding"):
        return False
    return int(existing.get("embedding_source") or 0) > endpoint_source_precedence(endpoint)


async def _resolve_company_link(
    db: AsyncIOMotorDatabase,
    derived: dict[str, Any],
    existing: dict[str, Any] | None,
    cache: dict[str, str | None] | None = None,
) -> None:
    """Resolve ``company_apollo_id`` -> canonical ``company_id`` (org doc Mongo _id).

    The user-facing ``company_id`` is the ORGANIZATION DOCUMENT's Mongo ``_id``
    (the id space the similarity company filter takes natively);
    ``company_apollo_id`` is kept on the doc as the resolution key so a pending
    link resolves on a later write once the org document exists. Re-resolves
    when the key changes (e.g. enrichment reveals a different employer);
    otherwise an established link is left untouched.
    """
    old = existing or {}
    key = derived.get("company_apollo_id") or old.get("company_apollo_id")
    if not key or (key == old.get("company_apollo_id") and old.get("company_id")):
        return
    if cache is not None and key in cache:
        resolved = cache[key]
    else:
        org = await db.leads.find_one(
            {"apollo_id": key, "entity_type": "organization"}, {"_id": 1}
        )
        resolved = str(org["_id"]) if org else None
        if cache is not None:
            cache[key] = resolved
    if resolved:
        derived["company_id"] = resolved


async def _upsert_search_records(
    db: AsyncIOMotorDatabase,
    *,
    entity_type: Literal["person", "organization"],
    records: list[dict[str, Any]],
    id_getter,
    endpoint: str,
    fallback_company_id: str | None = None,
) -> list[str]:
    """Merge each search hit into apollo_responses[endpoint]; return mongo `_id`s in hit order.

    ``fallback_company_id``: when a people search was scoped to exactly one
    organization, the request context supplies the Apollo org id even though
    ``mixed_people`` search hits are teaser-shaped (no ``organization_id``).
    It fills ``company_apollo_id`` (the resolution key) ONLY when neither the
    payload-derived fields nor the stored doc already carry one —
    enrichment-derived values always win. ``_resolve_company_link`` then turns
    the key into the canonical ``company_id`` (the org document's Mongo _id).
    """
    now = datetime.now(timezone.utc)
    mongo_ids: list[str] = []
    org_link_cache: dict[str, str | None] = {}

    def _apply_company_fallback(derived: dict[str, Any], existing_doc: dict[str, Any] | None) -> None:
        if (
            fallback_company_id
            and entity_type == "person"
            and "company_apollo_id" not in derived
            and not (existing_doc or {}).get("company_apollo_id")
        ):
            derived["company_apollo_id"] = fallback_company_id

    for record in records:
        if not isinstance(record, dict):
            continue
        apollo_id = id_getter(record)
        if not apollo_id:
            continue

        entry = endpoint_entry(record, now)
        # Up to 4 attempts. Two failure modes retry: (a) a concurrent stream
        # inserts the same apollo_id between our find and insert (unique index) —
        # retry lands on the update path; (b) the optimistic-concurrency guard
        # below matches 0 because a racing writer changed the doc — retry re-reads
        # the fuller doc and re-derives so the top-level fields converge.
        for _attempt in range(4):
            existing = await db.leads.find_one({"apollo_id": apollo_id})
            if existing:
                responses = responses_from_doc(existing)
                responses[endpoint] = entry
                # Derived top-level index fields recomputed from the merged
                # responses (per-field precedence => only upgrades, never regresses).
                derived = derive_top_fields(entity_type, responses)
                _apply_company_fallback(derived, existing)
                await _resolve_company_link(db, derived, existing, cache=org_link_cache)
                # Optimistic guard: write only if the doc still matches the snapshot
                # we derived from (updated_at + derived_at; equality-with-missing
                # covers legacy docs where derived_at is absent). A racing writer
                # bumps updated_at, so matched_count==0 => re-read, merge their entry,
                # re-derive from the fuller map. Residual: ms-precision timestamps
                # mean a same-millisecond write pair can slip the guard (vanishingly
                # narrow; self-heals on the next write).
                update_result = await db.leads.update_one(
                    {
                        "_id": existing["_id"],
                        "updated_at": existing.get("updated_at"),
                        "derived_at": existing.get("derived_at"),
                    },
                    {
                        # $set only THIS endpoint's entry via a dotted path (never
                        # the whole apollo_responses map) so a concurrent writer's
                        # entry for a different endpoint is not lost (lost-update
                        # race, e.g. enrich racing its own phone-reveal webhook).
                        "$set": {
                            "entity_type": entity_type,
                            f"apollo_responses.{endpoint}": entry,
                            "embedding": _embedding_flag_after_update(existing, endpoint),
                            "updated_at": now,
                            "derived_at": now,
                            **derived,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
                if update_result.matched_count == 0:
                    continue
                mongo_ids.append(str(existing["_id"]))
                break

            insert_derived = derive_top_fields(entity_type, {endpoint: entry})
            _apply_company_fallback(insert_derived, None)
            await _resolve_company_link(db, insert_derived, None, cache=org_link_cache)
            doc = {
                "apollo_id": apollo_id,
                "entity_type": entity_type,
                "apollo_responses": {endpoint: entry},
                "apollo_enriched": empty_enriched_flags(),
                "embedding": False,
                "created_at": now,
                "updated_at": now,
                "derived_at": now,
                **insert_derived,
            }
            try:
                result = await db.leads.insert_one(doc)
            except DuplicateKeyError:
                continue
            mongo_ids.append(str(result.inserted_id))
            break
        else:
            # Pathological contention: write ONLY the safe additive parts (payload
            # entry + embedding flag), never derived fields/derived_at, so a
            # stale-snapshot derived value can't overwrite a fresher one. Derived
            # converges on the next successful write.
            fallback = await db.leads.find_one({"apollo_id": apollo_id})
            if fallback:
                await db.leads.update_one(
                    {"_id": fallback["_id"]},
                    {
                        "$set": {
                            "entity_type": entity_type,
                            f"apollo_responses.{endpoint}": entry,
                            "embedding": _embedding_flag_after_update(fallback, endpoint),
                            "updated_at": now,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
                logger.warning(
                    "Optimistic guard exhausted for apollo_id %s; wrote payload "
                    "without re-deriving top-level fields (converges next write)",
                    apollo_id,
                )
                mongo_ids.append(str(fallback["_id"]))

    return mongo_ids


async def _upsert_enriched_record(
    db: AsyncIOMotorDatabase,
    *,
    entity_type: Literal["person", "organization"],
    apollo_id: str,
    apollo_response: dict[str, Any],
    endpoint: str,
    linkedin: bool = False,
    email: bool = False,
    phone: bool = False,
    index: bool = True,
) -> LeadOut:
    """Upsert enrichment payload. When ``index`` is False, skip embed/Milvus (caller streams it)."""
    now = datetime.now(timezone.utc)
    entry = endpoint_entry(apollo_response, now)

    doc: dict[str, Any] | None = None
    # Up to 4 attempts: covers the insert unique-index race AND the optimistic-
    # concurrency guard miss below (re-read merges the racer's entry + re-derives).
    for _attempt in range(4):
        existing = await db.leads.find_one({"apollo_id": apollo_id})
        if existing:
            responses = responses_from_doc(existing)
            responses[endpoint] = entry
            # flags recomputed per attempt from the fresh existing (OR-merge only).
            flags = merge_enriched_flags(
                normalize_apollo_enriched(existing, responses=responses),
                linkedin=linkedin,
                email=email,
                phone=phone,
            )
            derived = derive_top_fields(entity_type, responses)
            await _resolve_company_link(db, derived, existing)
            # Optimistic guard on the read snapshot (see _upsert_search_records):
            # matched_count==0 means a racing writer changed the doc => re-read.
            update_result = await db.leads.update_one(
                {
                    "_id": existing["_id"],
                    "updated_at": existing.get("updated_at"),
                    "derived_at": existing.get("derived_at"),
                },
                {
                    # $set only THIS endpoint's entry via a dotted path (never the
                    # whole apollo_responses map) so a concurrent writer's entry for
                    # a different endpoint is not lost (lost-update race).
                    "$set": {
                        "entity_type": entity_type,
                        f"apollo_responses.{endpoint}": entry,
                        "apollo_enriched": flags,
                        "embedding": _embedding_flag_after_update(existing, endpoint),
                        "updated_at": now,
                        "derived_at": now,
                        **derived,
                    },
                    "$unset": {"apollo_response": ""},
                },
            )
            if update_result.matched_count == 0:
                continue
            doc = await db.leads.find_one({"_id": existing["_id"]})
            break

        flags = merge_enriched_flags(
            None,
            linkedin=linkedin,
            email=email,
            phone=phone,
        )
        enrich_insert_derived = derive_top_fields(entity_type, {endpoint: entry})
        await _resolve_company_link(db, enrich_insert_derived, None)
        try:
            result = await db.leads.insert_one(
                {
                    "apollo_id": apollo_id,
                    "entity_type": entity_type,
                    "apollo_responses": {endpoint: entry},
                    "apollo_enriched": flags,
                    "embedding": False,
                    "created_at": now,
                    "updated_at": now,
                    "derived_at": now,
                    **enrich_insert_derived,
                }
            )
        except DuplicateKeyError:
            continue
        doc = await db.leads.find_one({"_id": result.inserted_id})
        break
    else:
        # Pathological contention: safe additive write only — the payload entry +
        # embedding flag, never derived fields/derived_at (nor apollo_enriched,
        # which an unguarded write could regress). Derived converges next write.
        fallback = await db.leads.find_one({"apollo_id": apollo_id})
        if fallback:
            await db.leads.update_one(
                {"_id": fallback["_id"]},
                {
                    "$set": {
                        "entity_type": entity_type,
                        f"apollo_responses.{endpoint}": entry,
                        "embedding": _embedding_flag_after_update(fallback, endpoint),
                        "updated_at": now,
                    },
                    "$unset": {"apollo_response": ""},
                },
            )
            logger.warning(
                "Optimistic guard exhausted for apollo_id %s; wrote payload without "
                "re-deriving top-level fields (converges next write)",
                apollo_id,
            )
            doc = await db.leads.find_one({"_id": fallback["_id"]})

    assert doc is not None
    if index:
        indexed = await index_lead_docs(
            [doc], source_precedence=endpoint_source_precedence(endpoint)
        )
        if indexed:
            await _mark_embedded(indexed)
        doc = await db.leads.find_one({"_id": doc["_id"]})
        assert doc is not None
    return _serialize_lead(doc)


@router.get("/health")
@anonymous("legacy health path (compose-era); k8s probes use /healthz + /readyz")
async def leads_health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "leads",
        "apollo_configured": settings.apollo_configured,
        "openai_configured": settings.openai_configured,
        "milvus_uri": settings.milvus_uri,
        "milvus_collection": settings.milvus_collection,
    }


@router.get("/stats")
async def leads_stats(db: AsyncIOMotorDatabase = Depends(get_database)) -> dict[str, Any]:
    """Collection-level counts for internal observability (e.g. the MCP server)."""
    total = await db.leads.count_documents({})
    people = await db.leads.count_documents({"entity_type": "person"})
    organizations = await db.leads.count_documents({"entity_type": "organization"})
    embedded = await db.leads.count_documents({"embedding": True})
    enriched_linkedin = await db.leads.count_documents({"apollo_enriched.linkedin": True})
    enriched_email = await db.leads.count_documents({"apollo_enriched.email": True})
    enriched_phone = await db.leads.count_documents({"apollo_enriched.phone": True})
    latest = await db.leads.find_one(sort=[("updated_at", -1)], projection={"updated_at": 1})
    return {
        "total_leads": total,
        "people": people,
        "organizations": organizations,
        "embedded": embedded,
        "embedding_pending": total - embedded,
        "enriched": {
            "linkedin": enriched_linkedin,
            "email": enriched_email,
            "phone": enriched_phone,
        },
        "last_updated_at": latest.get("updated_at") if latest else None,
    }


@router.get("/recent", response_model=list[LeadOut])
async def recent_leads(
    entity_type: Literal["person", "organization"] | None = Query(default=None),
    enriched: bool | None = Query(
        default=None,
        description="True: any enrichment flag set; False: no enrichment flags set",
    ),
    embedded: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    skip: int = Query(default=0, ge=0),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[LeadOut]:
    """Most recently updated leads — internal visibility into ingest/enrichment activity."""
    query: dict[str, Any] = {}
    if entity_type:
        query["entity_type"] = entity_type
    if embedded is not None:
        query["embedding"] = embedded
    if enriched is True:
        query["$or"] = [
            {"apollo_enriched.linkedin": True},
            {"apollo_enriched.email": True},
            {"apollo_enriched.phone": True},
        ]
    elif enriched is False:
        query["apollo_enriched.linkedin"] = {"$ne": True}
        query["apollo_enriched.email"] = {"$ne": True}
        query["apollo_enriched.phone"] = {"$ne": True}
    cursor = db.leads.find(query).sort("updated_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize_lead(doc) for doc in docs]


def _normalize_apollo_proxy_path(apollo_path: str) -> str:
    """Accept Apollo relative paths with or without the /api/v1 prefix."""
    cleaned = apollo_path.strip().lstrip("/")
    if cleaned.startswith("api/v1/"):
        cleaned = cleaned[len("api/v1/") :]
    if not cleaned or any(part == ".." for part in cleaned.split("/")):
        raise HTTPException(status_code=400, detail="Invalid Apollo API path")
    return cleaned


def _apollo_query_params_from_request(request: Request) -> dict[str, Any]:
    """Forward native Apollo query params; strip credentials and local control flags."""
    params: dict[str, Any] = {}
    for key in request.query_params.keys():
        lowered = key.lower()
        if lowered in _APOLLO_CREDENTIAL_KEYS or lowered in _APOLLO_CONTROL_KEYS:
            continue
        values = request.query_params.getlist(key)
        if not values:
            continue
        params[key] = values[0] if len(values) == 1 else values
    return params


def _merge_apollo_params(
    body: ApolloParamsBody | None,
    request: Request,
) -> dict[str, Any]:
    """Body params plus query params (query wins on key conflict), credentials stripped."""
    params = _params_dict(body)
    params.update(_apollo_query_params_from_request(request))
    return params


def _extract_stream_flag(
    body: ApolloParamsBody | None,
    request: Request,
) -> bool:
    """Read ``stream`` from body or query before it is stripped from Apollo params."""
    if body is not None:
        raw = body.model_dump(exclude_none=True).get("stream")
        if raw is not None:
            return _as_bool(raw)
    for key in request.query_params.keys():
        if key.lower() == "stream":
            values = request.query_params.getlist(key)
            if values:
                return _as_bool(values[0])
    return False


def _ndjson_line(payload: dict[str, Any]) -> str:
    # orjson serializes datetime natively; ``default=str`` preserves the prior
    # fallback for any other non-JSON-native value that slips into an event.
    return orjson.dumps(payload, default=str).decode() + "\n"


async def _ndjson_stream(stream_ids: list[str]) -> AsyncIterator[str]:
    async for event in iter_stream_events(stream_ids):
        yield _ndjson_line(event)


# Exclude reserved Apollo collection actions (e.g. people/match) from {id} routes.
_PERSON_BY_ID_RE = re.compile(r"^people/(?!match(?:/|$))([^/]+)$")
_ORG_BY_ID_RE = re.compile(r"^organizations/([^/]+)$")


def _combined_result_arrays(apollo_raw: dict[str, Any], *keys: str) -> list[Any]:
    combined: list[Any] = []
    for key in keys:
        value = apollo_raw.get(key)
        if isinstance(value, list):
            combined.extend(value)
    return combined


async def _fetch_people_search_page(
    settings: Settings,
    page_params: dict[str, Any],
    db: AsyncIOMotorDatabase | None = None,
) -> tuple[list[str], dict[str, Any], int]:
    database = db if db is not None else get_db()
    client = ApolloLeadsClient(settings)
    apollo_raw = await client.search_people(page_params)
    # Apollo mixed_people pages count "people" and "contacts" together; dropping
    # either loses hits and undercounts, which stops pagination early.
    people = [
        item
        for item in _combined_result_arrays(apollo_raw, "people", "contacts")
        if isinstance(item, dict)
    ]
    # Org-scoped searches know the company even though mixed_people hits are
    # teaser-shaped: exactly one organization_ids filter => context fallback for
    # the ingested people's derived company_id (enrichment-derived values win).
    org_ids = page_params.get("organization_ids")
    fallback_company_id = (
        str(org_ids[0]).strip()
        if isinstance(org_ids, list) and len(org_ids) == 1 and str(org_ids[0]).strip()
        else None
    )
    mongo_ids = await _upsert_search_records(
        database,
        entity_type="person",
        records=people,
        id_getter=_person_id_from_record,
        endpoint=PERSON_SEARCH,
        fallback_company_id=fallback_company_id,
    )
    return mongo_ids, apollo_raw, len(people)


async def _fetch_organizations_search_page(
    settings: Settings,
    page_params: dict[str, Any],
    db: AsyncIOMotorDatabase | None = None,
) -> tuple[list[str], dict[str, Any], int]:
    database = db if db is not None else get_db()
    client = ApolloLeadsClient(settings)
    apollo_raw = await client.search_organizations(page_params)
    # Every mixed_companies/search CALL costs 1 Apollo export credit, and an
    # exact-domain resolve fuzzy-matches ~10 pages of irrelevant orgs (a
    # 100-domain Prospect run burned ~1000 credits, 2026-08-12). A
    # domain-filtered query's match is on page 1 — stop the stream walk there.
    if page_params.get("q_organization_domains_list[]") or page_params.get(
        "q_organization_domains_list"
    ):
        for key in ("total_pages", "pagination"):
            apollo_raw.pop(key, None)
        apollo_raw["total_pages"] = 1
        pagination = apollo_raw.get("pagination")
        if isinstance(pagination, dict):
            pagination["total_pages"] = 1
    organizations = [
        item
        for item in _combined_result_arrays(apollo_raw, "organizations", "accounts")
        if isinstance(item, dict)
    ]
    mongo_ids = await _upsert_search_records(
        database,
        entity_type="organization",
        records=organizations,
        id_getter=_organization_id_from_record,
        endpoint=ORG_SEARCH,
    )
    return mongo_ids, apollo_raw, len(organizations)


async def _run_people_search_stream_job(
    settings: Settings,
    params: dict[str, Any],
    ingest_stream_id: str,
    embedding_stream_id: str,
) -> None:
    await run_paged_search_with_embedding(
        ingest_stream_id=ingest_stream_id,
        embedding_stream_id=embedding_stream_id,
        base_params=params,
        fetch_page=lambda page_params: _fetch_people_search_page(settings, page_params),
        embed_batch=lambda ids: _embed_mongo_ids_batch(
            ids, source_precedence=EMBED_SOURCE_SEARCH, nice=NICE_SEARCH_EMBED
        ),
    )


async def _run_organizations_search_stream_job(
    settings: Settings,
    params: dict[str, Any],
    ingest_stream_id: str,
    embedding_stream_id: str,
) -> None:
    await run_paged_search_with_embedding(
        ingest_stream_id=ingest_stream_id,
        embedding_stream_id=embedding_stream_id,
        base_params=params,
        fetch_page=lambda page_params: _fetch_organizations_search_page(settings, page_params),
        embed_batch=lambda ids: _embed_mongo_ids_batch(
            ids, source_precedence=EMBED_SOURCE_SEARCH, nice=NICE_SEARCH_EMBED
        ),
    )


async def _handle_people_search(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    params: dict[str, Any],
    *,
    stream: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    """Upsert people search results; optionally stream all Apollo pages."""
    if stream:
        ingest_job = await stream_job_manager.create()
        embedding_stream_id = await create_embedding_stream()
        if background_tasks is not None:
            background_tasks.add_task(
                _run_people_search_stream_job,
                settings,
                dict(params),
                ingest_job.stream_id,
                embedding_stream_id,
            )
        else:
            asyncio.create_task(
                _run_people_search_stream_job(
                    settings, dict(params), ingest_job.stream_id, embedding_stream_id
                )
            )
        return SearchIdsOut(
            ingest_stream_id=ingest_job.stream_id,
            embedding_stream_id=embedding_stream_id,
            ids=None,
        )

    mongo_ids, _, _ = await _fetch_people_search_page(settings, params, db=db)
    _schedule_background_embed(background_tasks, mongo_ids, source_precedence=EMBED_SOURCE_SEARCH)
    return SearchIdsOut(
        ingest_stream_id=None,
        embedding_stream_id=None,
        ids=mongo_ids,
    )


async def _handle_organizations_search(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    params: dict[str, Any],
    *,
    stream: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    """Upsert org search results; optionally stream all Apollo pages."""
    if stream:
        ingest_job = await stream_job_manager.create()
        embedding_stream_id = await create_embedding_stream()
        if background_tasks is not None:
            background_tasks.add_task(
                _run_organizations_search_stream_job,
                settings,
                dict(params),
                ingest_job.stream_id,
                embedding_stream_id,
            )
        else:
            asyncio.create_task(
                _run_organizations_search_stream_job(
                    settings, dict(params), ingest_job.stream_id, embedding_stream_id
                )
            )
        return SearchIdsOut(
            ingest_stream_id=ingest_job.stream_id,
            embedding_stream_id=embedding_stream_id,
            ids=None,
        )

    mongo_ids, _, _ = await _fetch_organizations_search_page(settings, params, db=db)
    _schedule_background_embed(background_tasks, mongo_ids, source_precedence=EMBED_SOURCE_SEARCH)
    return SearchIdsOut(
        ingest_stream_id=None,
        embedding_stream_id=None,
        ids=mongo_ids,
    )


async def _retrieve_and_upsert_person(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    apollo_id: str,
    params: dict[str, Any],
    *,
    index: bool,
) -> LeadOut:
    resolved_id = unquote(apollo_id).strip()
    if not resolved_id:
        raise HTTPException(status_code=400, detail="apollo_id is required in the path")
    client = ApolloLeadsClient(settings)
    apollo_response = await client.get_complete_person(resolved_id, params)
    person = _person_from_complete_response(apollo_response)
    resolved_id = _person_id_from_record(person) or resolved_id
    if not resolved_id:
        raise HTTPException(status_code=404, detail="Apollo person response missing id")
    return await _upsert_enriched_record(
        db,
        entity_type="person",
        apollo_id=resolved_id,
        apollo_response=apollo_response,
        endpoint=PERSON_BY_ID,
        linkedin=True,
        index=index,
    )


async def _retrieve_and_upsert_organization(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    apollo_id: str,
    params: dict[str, Any],
    *,
    index: bool,
) -> LeadOut:
    resolved_id = unquote(apollo_id).strip()
    if not resolved_id:
        raise HTTPException(status_code=400, detail="apollo_id is required in the path")
    client = ApolloLeadsClient(settings)
    apollo_response = await client.get_complete_organization(resolved_id, params)
    organization = _organization_from_complete_response(apollo_response)
    resolved_id = _organization_id_from_record(organization) or resolved_id
    if not resolved_id:
        raise HTTPException(status_code=404, detail="Apollo organization response missing id")
    return await _upsert_enriched_record(
        db,
        entity_type="organization",
        apollo_id=resolved_id,
        apollo_response=apollo_response,
        endpoint=ORG_BY_ID,
        linkedin=True,
        index=index,
    )


async def _run_enrich_stream_job(
    settings: Settings,
    *,
    entity_type: Literal["person", "organization"],
    apollo_ids: list[str],
    params: dict[str, Any],
    ingest_stream_id: str,
    embedding_stream_id: str,
) -> None:
    """Retrieve + upsert each id (ingest stream), embed concurrently (embedding stream)."""
    from app.stream_jobs import StreamJobStatus

    manager = stream_job_manager
    queue: asyncio.Queue[list[str] | None] = asyncio.Queue()
    unique_ids = list(dict.fromkeys(str(item).strip() for item in apollo_ids if str(item or "").strip()))
    total = len(unique_ids)
    embed_sem = asyncio.Semaphore(4)

    async def _ingest() -> None:
        job = manager.get(ingest_stream_id)
        if not job:
            await queue.put(None)
            return
        job.status = StreamJobStatus.RUNNING
        db = get_db()
        failed = 0
        try:
            for index, apollo_id in enumerate(unique_ids, start=1):
                if job.cancelled:
                    break
                try:
                    if entity_type == "person":
                        lead = await _retrieve_and_upsert_person(
                            db, settings, apollo_id, params, index=False
                        )
                    else:
                        lead = await _retrieve_and_upsert_organization(
                            db, settings, apollo_id, params, index=False
                        )
                except Exception as exc:
                    # Per-item failure: report it and continue with the remaining
                    # ids instead of aborting the whole batch. "error" is terminal
                    # for subscribers, so a distinct event type is used.
                    logger.exception("Enrich retrieve failed for %s", apollo_id)
                    failed += 1
                    await manager.publish(
                        ingest_stream_id,
                        {
                            "type": "item_error",
                            "kind": "ingest",
                            "page": index,
                            "total_pages": total,
                            "apollo_id": apollo_id,
                            # Sanitized code only (full exception logged above); the
                            # apollo_id rides its own field. Raw str(exc) here reached
                            # browsers verbatim via search — a backend-internals leak.
                            "detail": _safe_ingest_error_detail(exc),
                        },
                    )
                    continue
                mongo_id = lead.id
                job.total_ids += 1
                await queue.put([mongo_id])
                await manager.publish(
                    ingest_stream_id,
                    {
                        "type": "ids",
                        "kind": "ingest",
                        "page": index,
                        "total_pages": total,
                        "ids": [mongo_id],
                        "stored": job.total_ids,
                    },
                )
            await manager.publish(
                ingest_stream_id,
                {
                    "type": "complete",
                    "kind": "ingest",
                    "total": job.total_ids,
                    "pages": total,
                    "failed": failed,
                    "cancelled": job.cancelled,
                },
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.COMPLETE)
        except Exception as exc:
            logger.exception("Enrich stream job %s failed", ingest_stream_id)
            # Sanitized code only — the full exception is logged above; this detail
            # is relayed verbatim to browsers by search.
            detail = _safe_ingest_error_detail(exc)
            await manager.publish(
                ingest_stream_id,
                {"type": "error", "kind": "ingest", "detail": detail},
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.ERROR, error=detail)
        finally:
            await queue.put(None)

    async def _embed_consumer() -> None:
        tasks: set[asyncio.Task[None]] = set()

        async def _one(batch: list[str]) -> None:
            async with embed_sem:
                await schedule_embedding_batch(
                    embedding_stream_id,
                    batch,
                    embed_batch=lambda ids: _embed_mongo_ids_batch(
                        ids, source_precedence=EMBED_SOURCE_COMPLETE_INFO, nice=NICE_SEARCH_EMBED
                    ),
                )

        try:
            while True:
                batch = await queue.get()
                if batch is None:
                    break
                task = asyncio.create_task(
                    _one(batch),
                    name=f"enrich-embed-{embedding_stream_id[:8]}",
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await close_embedding_stream(embedding_stream_id)

    await asyncio.gather(_ingest(), _embed_consumer())


async def _start_enrich_stream(
    settings: Settings,
    *,
    entity_type: Literal["person", "organization"],
    apollo_ids: list[str],
    params: dict[str, Any],
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    unique_ids = list(dict.fromkeys(str(item).strip() for item in apollo_ids if str(item or "").strip()))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="At least one apollo id is required")
    ingest_job = await stream_job_manager.create()
    embedding_stream_id = await create_embedding_stream()
    if background_tasks is not None:
        background_tasks.add_task(
            _run_enrich_stream_job,
            settings,
            entity_type=entity_type,
            apollo_ids=unique_ids,
            params=dict(params),
            ingest_stream_id=ingest_job.stream_id,
            embedding_stream_id=embedding_stream_id,
        )
    else:
        asyncio.create_task(
            _run_enrich_stream_job(
                settings,
                entity_type=entity_type,
                apollo_ids=unique_ids,
                params=dict(params),
                ingest_stream_id=ingest_job.stream_id,
                embedding_stream_id=embedding_stream_id,
            )
        )
    return SearchIdsOut(
        ingest_stream_id=ingest_job.stream_id,
        embedding_stream_id=embedding_stream_id,
        ids=None,
    )


async def _handle_people_enrich(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    apollo_id: str,
    params: dict[str, Any],
    *,
    stream: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    """Same response shape as mixed_people search: stream handles and/or mongo ids."""
    resolved_id = unquote(apollo_id).strip()
    if not resolved_id:
        raise HTTPException(status_code=400, detail="apollo_id is required in the path")
    if stream:
        return await _start_enrich_stream(
            settings,
            entity_type="person",
            apollo_ids=[resolved_id],
            params=params,
            background_tasks=background_tasks,
        )
    lead = await _retrieve_and_upsert_person(
        db, settings, resolved_id, params, index=False
    )
    _schedule_background_embed(
        background_tasks, [lead.id], source_precedence=EMBED_SOURCE_COMPLETE_INFO
    )
    return SearchIdsOut(
        ingest_stream_id=None,
        embedding_stream_id=None,
        ids=[lead.id],
    )


async def _handle_organizations_enrich(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    apollo_id: str,
    params: dict[str, Any],
    *,
    stream: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    """Same response shape as mixed_companies search: stream handles and/or mongo ids."""
    resolved_id = unquote(apollo_id).strip()
    if not resolved_id:
        raise HTTPException(status_code=400, detail="apollo_id is required in the path")
    if stream:
        return await _start_enrich_stream(
            settings,
            entity_type="organization",
            apollo_ids=[resolved_id],
            params=params,
            background_tasks=background_tasks,
        )
    lead = await _retrieve_and_upsert_organization(
        db, settings, resolved_id, params, index=False
    )
    _schedule_background_embed(
        background_tasks, [lead.id], source_precedence=EMBED_SOURCE_COMPLETE_INFO
    )
    return SearchIdsOut(
        ingest_stream_id=None,
        embedding_stream_id=None,
        ids=[lead.id],
    )


def _match_apollo_ids_from_params(params: dict[str, Any]) -> list[str]:
    """Collect apollo person ids from ``ids`` / ``id`` / ``person_id`` (order preserved)."""
    ids: list[str] = []
    raw_ids = params.pop("ids", None)
    if isinstance(raw_ids, list):
        ids.extend(str(item).strip() for item in raw_ids if str(item or "").strip())
    elif raw_ids is not None and str(raw_ids).strip():
        ids.append(str(raw_ids).strip())
    for key in ("id", "person_id"):
        value = params.get(key)
        if value is not None and str(value).strip():
            ids.append(str(value).strip())
    return list(dict.fromkeys(ids))


def _prepare_people_match_params(
    settings: Settings,
    params: dict[str, Any],
) -> tuple[dict[str, Any], bool, bool, bool]:
    """Normalize match flags and inject webhook URL.

    Returns ``(apollo_params, run_waterfall_email, run_waterfall_phone, reveal_phone)``.
    Client-supplied ``webhook_url`` is ignored.
    """
    out = dict(params)
    out.pop("webhook_url", None)
    out.pop("stream", None)
    out.pop("ids", None)

    run_waterfall_email = _as_bool(out.get("run_waterfall_email"))
    run_waterfall_phone = _as_bool(out.get("run_waterfall_phone"))
    reveal_phone = _as_bool(out.get("reveal_phone_number"))
    needs_webhook = run_waterfall_email or run_waterfall_phone or reveal_phone

    if run_waterfall_email:
        out["run_waterfall_email"] = True
    else:
        out.pop("run_waterfall_email", None)
    if run_waterfall_phone:
        out["run_waterfall_phone"] = True
    else:
        out.pop("run_waterfall_phone", None)

    if needs_webhook:
        try:
            out["webhook_url"] = settings.people_match_async_webhook_url()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    return out, run_waterfall_email, run_waterfall_phone, reveal_phone


def _waterfall_pending_from_response(
    apollo_response: dict[str, Any],
    *,
    run_waterfall_email: bool,
    run_waterfall_phone: bool,
) -> bool:
    waterfall_pending = run_waterfall_email or run_waterfall_phone
    waterfall_status = apollo_response.get("waterfall")
    if isinstance(waterfall_status, dict):
        status_value = str(waterfall_status.get("status") or "").lower()
        if status_value in {"failed", "error"}:
            return False
    return waterfall_pending


async def _match_and_upsert_person(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    apollo_id: str | None,
    base_params: dict[str, Any],
    *,
    run_waterfall_email: bool,
    run_waterfall_phone: bool,
    reveal_phone: bool,
    index: bool = False,
) -> tuple[LeadOut, bool, bool]:
    """Call Apollo people/match; upsert without sync embed when ``index`` is False.

    ``apollo_id`` may be omitted when ``base_params`` already has Apollo identity fields.

    Returns ``(lead, phone_reveal_pending, waterfall_pending)``.
    """
    params = dict(base_params)
    resolved = str(apollo_id or "").strip()
    if resolved:
        params["id"] = resolved

    client = ApolloLeadsClient(settings)
    apollo_response = await client.match_person(params)
    person = _person_from_complete_response(apollo_response)
    person_apollo_id = _person_id_from_record(person) or resolved
    if not person_apollo_id:
        raise HTTPException(status_code=404, detail="Apollo people/match response missing person id")

    lead = await _upsert_enriched_record(
        db,
        entity_type="person",
        apollo_id=person_apollo_id,
        apollo_response=apollo_response,
        endpoint=PERSON_MATCH,
        email=run_waterfall_email,
        phone=run_waterfall_phone or reveal_phone,
        index=index,
    )
    return (
        lead,
        reveal_phone or run_waterfall_phone,
        _waterfall_pending_from_response(
            apollo_response,
            run_waterfall_email=run_waterfall_email,
            run_waterfall_phone=run_waterfall_phone,
        ),
    )


async def _run_match_stream_job(
    settings: Settings,
    *,
    apollo_ids: list[str],
    params: dict[str, Any],
    run_waterfall_email: bool,
    run_waterfall_phone: bool,
    reveal_phone: bool,
    ingest_stream_id: str,
    embedding_stream_id: str,
) -> None:
    """Match + upsert each id (ingest stream), embed concurrently (embedding stream)."""
    from app.stream_jobs import StreamJobStatus

    manager = stream_job_manager
    queue: asyncio.Queue[list[str] | None] = asyncio.Queue()
    unique_ids = list(dict.fromkeys(str(item).strip() for item in apollo_ids if str(item or "").strip()))
    total = len(unique_ids)
    embed_sem = asyncio.Semaphore(4)

    async def _ingest() -> None:
        job = manager.get(ingest_stream_id)
        if not job:
            await queue.put(None)
            return
        job.status = StreamJobStatus.RUNNING
        db = get_db()
        failed = 0
        try:
            for index, apollo_id in enumerate(unique_ids, start=1):
                if job.cancelled:
                    break
                try:
                    lead, phone_pending, waterfall_pending = await _match_and_upsert_person(
                        db,
                        settings,
                        apollo_id,
                        params,
                        run_waterfall_email=run_waterfall_email,
                        run_waterfall_phone=run_waterfall_phone,
                        reveal_phone=reveal_phone,
                        index=False,
                    )
                except Exception as exc:
                    # Per-item failure: report and continue (see enrich job note).
                    logger.exception("People match failed for %s", apollo_id)
                    failed += 1
                    await manager.publish(
                        ingest_stream_id,
                        {
                            "type": "item_error",
                            "kind": "ingest",
                            "page": index,
                            "total_pages": total,
                            "apollo_id": apollo_id,
                            # Sanitized code only (full exception logged above); the
                            # apollo_id rides its own field. Raw str(exc) here reached
                            # browsers verbatim via search — a backend-internals leak.
                            "detail": _safe_ingest_error_detail(exc),
                        },
                    )
                    continue
                mongo_id = lead.id
                job.total_ids += 1
                await queue.put([mongo_id])
                await manager.publish(
                    ingest_stream_id,
                    {
                        "type": "ids",
                        "kind": "ingest",
                        "page": index,
                        "total_pages": total,
                        "ids": [mongo_id],
                        "stored": job.total_ids,
                        "phone_reveal_pending": phone_pending,
                        "waterfall_pending": waterfall_pending,
                    },
                )
            await manager.publish(
                ingest_stream_id,
                {
                    "type": "complete",
                    "kind": "ingest",
                    "total": job.total_ids,
                    "pages": total,
                    "failed": failed,
                    "cancelled": job.cancelled,
                },
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.COMPLETE)
        except Exception as exc:
            logger.exception("Match stream job %s failed", ingest_stream_id)
            # Sanitized code only — the full exception is logged above; this detail
            # is relayed verbatim to browsers by search.
            detail = _safe_ingest_error_detail(exc)
            await manager.publish(
                ingest_stream_id,
                {"type": "error", "kind": "ingest", "detail": detail},
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.ERROR, error=detail)
        finally:
            await queue.put(None)

    async def _embed_consumer() -> None:
        tasks: set[asyncio.Task[None]] = set()

        async def _one(batch: list[str]) -> None:
            async with embed_sem:
                await schedule_embedding_batch(
                    embedding_stream_id,
                    batch,
                    embed_batch=lambda ids: _embed_mongo_ids_batch(
                        ids, source_precedence=EMBED_SOURCE_MATCH, nice=NICE_SEARCH_EMBED
                    ),
                )

        try:
            while True:
                batch = await queue.get()
                if batch is None:
                    break
                task = asyncio.create_task(
                    _one(batch),
                    name=f"match-embed-{embedding_stream_id[:8]}",
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await close_embedding_stream(embedding_stream_id)

    await asyncio.gather(_ingest(), _embed_consumer())


async def _start_match_stream(
    settings: Settings,
    *,
    apollo_ids: list[str],
    params: dict[str, Any],
    run_waterfall_email: bool,
    run_waterfall_phone: bool,
    reveal_phone: bool,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    unique_ids = list(dict.fromkeys(str(item).strip() for item in apollo_ids if str(item or "").strip()))
    if not unique_ids:
        raise HTTPException(status_code=400, detail="At least one apollo id is required")
    ingest_job = await stream_job_manager.create()
    embedding_stream_id = await create_embedding_stream()
    if background_tasks is not None:
        background_tasks.add_task(
            _run_match_stream_job,
            settings,
            apollo_ids=unique_ids,
            params=dict(params),
            run_waterfall_email=run_waterfall_email,
            run_waterfall_phone=run_waterfall_phone,
            reveal_phone=reveal_phone,
            ingest_stream_id=ingest_job.stream_id,
            embedding_stream_id=embedding_stream_id,
        )
    else:
        asyncio.create_task(
            _run_match_stream_job(
                settings,
                apollo_ids=unique_ids,
                params=dict(params),
                run_waterfall_email=run_waterfall_email,
                run_waterfall_phone=run_waterfall_phone,
                reveal_phone=reveal_phone,
                ingest_stream_id=ingest_job.stream_id,
                embedding_stream_id=embedding_stream_id,
            )
        )
    return SearchIdsOut(
        ingest_stream_id=ingest_job.stream_id,
        embedding_stream_id=embedding_stream_id,
        ids=None,
    )


async def _handle_people_match(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    params: dict[str, Any],
    *,
    stream: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> SearchIdsOut:
    """POST Apollo people/match; same ``SearchIdsOut`` shape as search/enrich.

    Person identity from ``ids`` / ``id`` / ``person_id`` (and Apollo email keys).

    Async delivery (requires PUBLIC_BASE_URL webhook):
    - ``run_waterfall_email`` / ``run_waterfall_phone`` — waterfall enrichment
    - ``reveal_phone_number`` — native Apollo phone reveal

    With ``stream=true``, pending webhook flags are published on ingest ``ids`` events.
    """
    apollo_ids = _match_apollo_ids_from_params(params)
    prepared, run_waterfall_email, run_waterfall_phone, reveal_phone = _prepare_people_match_params(
        settings, params
    )

    if stream:
        if not apollo_ids:
            raise HTTPException(status_code=400, detail="At least one apollo id is required")
        return await _start_match_stream(
            settings,
            apollo_ids=apollo_ids,
            params=prepared,
            run_waterfall_email=run_waterfall_email,
            run_waterfall_phone=run_waterfall_phone,
            reveal_phone=reveal_phone,
            background_tasks=background_tasks,
        )

    # Non-stream: one person, return mongo id; embedding runs on a tracked stream.
    target_id = apollo_ids[0] if apollo_ids else None
    if not target_id and not any(
        prepared.get(key) for key in ("email", "first_name", "last_name", "linkedin_url")
    ):
        raise HTTPException(
            status_code=400,
            detail="people/match requires id, person_id, ids, or Apollo identity fields",
        )
    lead, _phone_pending, _waterfall_pending = await _match_and_upsert_person(
        db,
        settings,
        target_id,
        prepared,
        run_waterfall_email=run_waterfall_email,
        run_waterfall_phone=run_waterfall_phone,
        reveal_phone=reveal_phone,
        index=False,
    )
    _schedule_background_embed(background_tasks, [lead.id], source_precedence=EMBED_SOURCE_MATCH)
    return SearchIdsOut(
        ingest_stream_id=None,
        embedding_stream_id=None,
        ids=[lead.id],
    )


@router.post("/exists")
async def leads_exist(
    body: BatchExistsRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict[str, Any]:
    """Return which of the given Mongo `_id`s exist (projection-only, no payloads).

    Backs CSV re-import: validating 100k ids must not hydrate 100k docs.
    """
    present: list[str] = []
    seen: set[str] = set()
    valid: list[ObjectId] = []
    for value in body.ids:
        raw = (value or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            valid.append(ObjectId(raw))
        except Exception:
            continue
    for start in range(0, len(valid), 10000):
        chunk = valid[start : start + 10000]
        async for doc in db.leads.find({"_id": {"$in": chunk}}, {"_id": 1}):
            present.append(str(doc["_id"]))
    return {"present": present}


@router.get("/organizations/resolve", response_model=LeadOut)
async def resolve_organization_lead(
    value: str = Query(min_length=1, description="Company record Mongo _id or Apollo org id"),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> LeadOut:
    """Resolve either company id space to the stored organization lead (display shape).

    Backs the search UI's company-filter chips: typing a verbatim id resolves to
    the org doc so the chip can show the company NAME. Mongo-only (never Apollo).
    """
    org_doc: dict[str, Any] | None = None
    try:
        oid = ObjectId(value.strip())
    except Exception:
        oid = None
    if oid is not None:
        candidate = await db.leads.find_one({"_id": oid})
        if candidate and (candidate.get("entity_type") or "person") == "organization":
            org_doc = candidate
    if org_doc is None:
        org_doc = await db.leads.find_one(
            {"apollo_id": value.strip(), "entity_type": "organization"}
        )
    if org_doc is None:
        # Local-first domain resolution: fuzzy org-search walks ingested huge
        # numbers of orgs, so a domain often resolves from Mongo for free.
        org_doc = await db.leads.find_one(
            {"domain": value.strip().lower(), "entity_type": "organization"}
        )
    if org_doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No organization lead found for {value} "
                "(accepts a company record's Mongo id or Apollo organization id)"
            ),
        )
    return _serialize_lead(org_doc, "display")


@router.get("/apollo/{apollo_path:path}")
async def apollo_proxy_get(
    apollo_path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> Any:
    """GET Apollo relative path with native query params.

    Known paths also upsert into Mongo and return ``SearchIdsOut`` (same as search):
    - ``api/v1/people/{id}`` — complete person info (``stream=true`` → stream ids; else mongo ids)
    - ``api/v1/organizations/{id}`` — complete organization info (same)

    Other GET paths (e.g. ``api/v1/users/api_profile``) are proxied as-is.
    """
    path = _normalize_apollo_proxy_path(apollo_path)
    stream = _extract_stream_flag(None, request)
    params = _apollo_query_params_from_request(request)

    person_match = _PERSON_BY_ID_RE.fullmatch(path)
    if person_match:
        return await _handle_people_enrich(
            db,
            settings,
            person_match.group(1),
            params,
            stream=stream,
            background_tasks=background_tasks,
        )

    org_match = _ORG_BY_ID_RE.fullmatch(path)
    if org_match:
        return await _handle_organizations_enrich(
            db,
            settings,
            org_match.group(1),
            params,
            stream=stream,
            background_tasks=background_tasks,
        )

    client = ApolloLeadsClient(settings)
    return await client.proxy_get(path, params=params)


@router.post("/apollo/{apollo_path:path}")
async def apollo_proxy_post(
    apollo_path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    body: ApolloParamsBody | None = Body(default=None),
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> Any:
    """POST Apollo relative path with native params (JSON body and/or query).

    Known paths also upsert into Mongo (search embeds in background):
    - ``api/v1/mixed_people/api_search`` → ``SearchIdsOut`` (optional ``stream``)
    - ``api/v1/mixed_companies/search`` → ``SearchIdsOut`` (optional ``stream``)
    - ``api/v1/people/match`` → ``SearchIdsOut`` (optional ``stream``; pending flags on ingest ``ids`` events)
    """
    path = _normalize_apollo_proxy_path(apollo_path)
    stream = _extract_stream_flag(body, request)
    params = _merge_apollo_params(body, request)

    if path == "mixed_people/api_search":
        return await _handle_people_search(
            db, settings, params, stream=stream, background_tasks=background_tasks
        )
    if path == "mixed_companies/search":
        return await _handle_organizations_search(
            db, settings, params, stream=stream, background_tasks=background_tasks
        )
    if path == "people/match":
        return await _handle_people_match(
            db,
            settings,
            params,
            stream=stream,
            background_tasks=background_tasks,
        )

    raise HTTPException(
        status_code=404,
        detail=f"Unsupported Apollo POST path: /api/v1/{path}",
    )


# Default: docs matched over this count require an explicit confirm=true. A full
# embed of the collection is an expensive OpenAI + Milvus pass (Principle 4).
_BACKFILL_CONFIRM_DOCS_DEFAULT = 5000.0
# Mongo _id pages fed into the embedding stream; each is embedded in smaller
# OpenAI sub-batches internally (see schedule_embedding_batch).
_BACKFILL_BATCH_SIZE = 500


def _backfill_query(force: bool) -> dict[str, Any]:
    """Docs to embed: all when ``force`` (re-embed), else those not yet embedded.

    ``{"embedding": {"$ne": True}}`` also catches legacy docs missing the field.
    """
    return {} if force else {"embedding": {"$ne": True}}


async def _run_embedding_backfill(
    *,
    query: dict[str, Any],
    force: bool,
    embedding_stream_id: str,
) -> None:
    """Stream Mongo _id pages through the embedding infra, then close the stream.

    Reuses ``schedule_embedding_batch`` so progress publishes on the embedding
    stream and the ``embedding`` flag flips (via ``_mark_embedded``) only after
    Milvus indexing succeeds — the same invariant as live search embedding.
    """
    db = get_db()
    try:
        batch: list[str] = []
        cursor = db.leads.find(query, projection={"_id": 1}).sort("_id", 1)
        async for doc in cursor:
            batch.append(str(doc["_id"]))
            if len(batch) >= _BACKFILL_BATCH_SIZE:
                await schedule_embedding_batch(
                    embedding_stream_id,
                    batch,
                    embed_batch=lambda ids: _embed_mongo_ids_batch(
                        ids, source_precedence=0, force=force, nice=NICE_BACKFILL
                    ),
                )
                batch = []
        if batch:
            await schedule_embedding_batch(
                embedding_stream_id,
                batch,
                embed_batch=lambda ids: _embed_mongo_ids_batch(
                    ids, source_precedence=0, force=force, nice=NICE_BACKFILL
                ),
            )
    except Exception:
        logger.exception("Embedding backfill failed for stream %s", embedding_stream_id)
    finally:
        # Marks the embedding stream done (publishes complete) even on early exit.
        await close_embedding_stream(embedding_stream_id)


@router.post("/embeddings/backfill", response_model=EmbeddingBackfillResponse)
async def embeddings_backfill(
    background_tasks: BackgroundTasks,
    force: bool = Query(
        default=False,
        description="Re-embed all leads (else only those with embedding != true).",
    ),
    confirm: bool = Query(
        default=False,
        description="Proceed past the confirmation gate for a large backfill.",
    ),
    confirm_token: str | None = Query(default=None),
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> EmbeddingBackfillResponse:
    """Embed leads missing a vector (or all, with ``force``) → OpenAI → Milvus.

    Expensive-action gate (Principle 4): estimates the matched doc count first;
    over the configurable threshold it returns ``409 confirmation_required`` with
    the estimate and a ``confirm_token``. Re-invoke with ``confirm=true`` to run.

    Reuses the embedding-stream infra: returns an ``embedding_stream_id`` to
    subscribe to for progress. The ``embedding`` flag flips to True per doc only
    after Milvus indexing succeeds.
    """
    if not settings.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured; cannot backfill embeddings",
        )

    query = _backfill_query(force)
    matched = await db.leads.count_documents(query)

    threshold = confirmation_threshold(
        "LEADS_BACKFILL_CONFIRM_DOCS", _BACKFILL_CONFIRM_DOCS_DEFAULT
    )
    require_confirmation(
        float(matched),
        threshold,
        confirm=confirm,
        confirm_token=confirm_token,
        verify_token=True,
        unit="documents",
        action="embeddings_backfill" + (":force" if force else ""),
        message=(
            f"Embedding backfill would (re-)embed {matched} lead(s), exceeding the "
            f"{int(threshold)}-document threshold; re-invoke with confirm=true to proceed."
        ),
        meta={"force": force},
    )

    if matched == 0:
        return EmbeddingBackfillResponse(embedding_stream_id=None, matched=0, force=force)

    embedding_stream_id = await create_embedding_stream()
    background_tasks.add_task(
        _run_embedding_backfill,
        query=query,
        force=force,
        embedding_stream_id=embedding_stream_id,
    )
    return EmbeddingBackfillResponse(
        embedding_stream_id=embedding_stream_id,
        matched=matched,
        force=force,
    )


@router.get("/stream/{stream_id}")
async def stream_one(
    stream_id: str,
) -> StreamingResponse:
    """NDJSON stream for a single search job (``ids`` / ``complete`` / ``error`` events)."""
    cleaned = stream_id.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="stream_id is required")
    if stream_job_manager.get(cleaned) is None:
        raise HTTPException(status_code=404, detail="Unknown or expired stream_id")
    return StreamingResponse(
        _ndjson_stream([cleaned]),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stream/{stream_id}/cancel")
async def stream_cancel(
    stream_id: str,
) -> dict[str, object]:
    """Cancel a running ingest or embedding stream job."""
    cleaned = stream_id.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="stream_id is required")
    cancelled = await cancel_stream(cleaned)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Unknown, finished, or expired stream_id")
    return {"stream_id": cleaned, "cancelled": True}


# Actions the search service may issue against the leads stream engine. leads is
# the ENGINE behind search's jobs, not a `jobs` producer: this is the internal
# control hook search calls to pause/resume/cancel the underlying ingest or
# embedding stream. Authorization is the standard search->leads path — the call
# carries a leads-audience token (RFC 8693 exchange) and rides the same
# `/api/leads` grant as every other search->leads request. It is deliberately NOT
# a `/internal/jobs/v1/*` route (leads does not publish to the jobs service).
_STREAM_CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel"})


@router.post("/stream/{stream_id}/control/{action}", response_model=StreamControlResponse)
async def stream_control(
    stream_id: str,
    action: str,
) -> StreamControlResponse:
    """Pause, resume, or cancel a running ingest or embedding stream job.

    Idempotent: re-issuing an action that is already in effect returns
    ``applied=false`` with the current ``status`` (still 200). Unknown/finished
    streams 404. Pausing an ingest stream stops it before the next Apollo page so
    it stops spending credits; resuming continues from where it left off.
    """
    cleaned = stream_id.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="stream_id is required")
    normalized = action.strip().lower()
    if normalized not in _STREAM_CONTROL_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported control action {action!r}; expected pause, resume, or cancel",
        )

    if normalized == "cancel":
        before = stream_status(cleaned)
        cancelled = await cancel_stream(cleaned)
        if not cancelled and before is None:
            raise HTTPException(
                status_code=404, detail="Unknown, finished, or expired stream_id"
            )
        return StreamControlResponse(
            stream_id=cleaned,
            action=normalized,
            status=stream_status(cleaned) or "canceled",
            applied=cancelled,
        )

    before = stream_status(cleaned)
    found, label = await set_stream_paused(cleaned, paused=(normalized == "pause"))
    if not found and label is None:
        raise HTTPException(status_code=404, detail="Unknown, finished, or expired stream_id")
    target = "paused" if normalized == "pause" else "running"
    applied = bool(found) and before != target and label == target
    return StreamControlResponse(
        stream_id=cleaned,
        action=normalized,
        status=label or target,
        applied=applied,
    )


@router.post("/stream")
async def stream_many(
    body: StreamSubscribeRequest,
) -> StreamingResponse:
    """Multiplex many stream jobs onto one NDJSON connection.

    Every event includes ``stream_id``. Unknown ids emit a single ``error`` event
    and are otherwise ignored; the connection closes when all known jobs finish.
    """
    stream_ids = [str(item or "").strip() for item in body.stream_ids if str(item or "").strip()]
    if not stream_ids:
        raise HTTPException(status_code=400, detail="stream_ids must not be empty")
    return StreamingResponse(
        _ndjson_stream(stream_ids),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _merge_async_person_into_match_data(
    data: dict[str, Any],
    entry: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Merge phone-reveal or waterfall webhook person entry into stored match data."""
    data = dict(data)
    data["async_webhook"] = payload
    data["async_webhook_person"] = entry
    # Keep legacy keys for older UI / debugging.
    data["phone_webhook"] = payload
    data["phone_webhook_person"] = entry

    person = data.get("person")
    person = dict(person) if isinstance(person, dict) else {}

    phone_numbers = entry.get("phone_numbers")
    if isinstance(phone_numbers, list):
        person["phone_numbers"] = phone_numbers
        data["phone_numbers"] = phone_numbers

    emails = entry.get("emails")
    if isinstance(emails, list) and emails:
        person["emails"] = emails
        data["emails"] = emails
        first = emails[0]
        if isinstance(first, dict):
            email_value = first.get("email")
            if email_value:
                person["email"] = email_value
        elif isinstance(first, str) and first.strip():
            person["email"] = first.strip()

    email_value = entry.get("email")
    if isinstance(email_value, str) and email_value.strip():
        person["email"] = email_value.strip()

    if isinstance(entry.get("waterfall"), dict):
        person["waterfall"] = entry["waterfall"]
        data["waterfall_result"] = entry["waterfall"]

    if person:
        data["person"] = person
    return data


@router.post("/webhooks/apollo")
@router.post("/webhooks/apollo/{secret}")
@anonymous(
    "Apollo async webhook delivery — Apollo cannot send bearer tokens; "
    "authenticated by a constant-time secret-in-path/query compare, 503s "
    "when the secret is unconfigured"
)
async def apollo_people_match_webhook(
    request: Request,
    secret: str | None = None,
    secret_query: str | None = Query(default=None, alias="secret"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Receive Apollo async people-match payloads (phone reveal and/or waterfall email/phone)."""
    if not settings.apollo_webhook_secret_configured:
        # This route is publicly reachable (nginx, no JWT); serving it with the
        # repo-published placeholder secret would let anyone forge lead data.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APOLLO_WEBHOOK_SECRET is not configured",
        )
    provided = (secret or secret_query or "").strip()
    if not provided or not secrets.compare_digest(
        provided.encode(), settings.apollo_webhook_secret.encode()
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be a JSON object")

    people = payload.get("people")
    if not isinstance(people, list):
        people = []

    updated_ids: list[str] = []
    now = datetime.now(timezone.utc)
    for entry in people:
        if not isinstance(entry, dict):
            continue
        apollo_id = str(entry.get("id") or "").strip()
        if not apollo_id:
            continue

        doc = None
        # Up to 4 attempts: covers the insert unique-index race AND the optimistic-
        # concurrency guard miss below (re-read merges the racer's match data +
        # re-derives so the top-level fields converge).
        for _attempt in range(4):
            existing = await db.leads.find_one({"apollo_id": apollo_id})
            responses = responses_from_doc(existing) if existing else {}
            match_entry = responses.get(PERSON_MATCH)
            if isinstance(match_entry, dict) and isinstance(match_entry.get("data"), dict):
                data = dict(match_entry["data"])
            else:
                data = {}

            data = _merge_async_person_into_match_data(data, entry, payload=payload)
            new_match_entry = endpoint_entry(data, now)
            responses[PERSON_MATCH] = new_match_entry
            targets = payload.get("target_fields")
            target_set = (
                {str(item).lower() for item in targets}
                if isinstance(targets, list)
                else set()
            )
            has_email_payload = bool(
                entry.get("email")
                or (isinstance(entry.get("emails"), list) and entry.get("emails"))
                or "emails" in target_set
                or "email" in target_set
            )
            has_phone_payload = bool(
                (isinstance(entry.get("phone_numbers"), list) and entry.get("phone_numbers"))
                or "phone_numbers" in target_set
                or "phones" in target_set
                or "phone" in target_set
            )
            flags = merge_enriched_flags(
                normalize_apollo_enriched(existing or {}, responses=responses),
                email=has_email_payload,
                phone=has_phone_payload,
            )
            # Async phone-reveal / waterfall payload upserts email/phone/linkedin
            # onto the already-stored doc (the §1.3 upsert guarantee).
            derived = derive_top_fields("person", responses)
            await _resolve_company_link(db, derived, existing)
            if existing:
                # Optimistic guard on the read snapshot (see _upsert_search_records):
                # matched_count==0 means a racing writer changed the doc => re-read.
                update_result = await db.leads.update_one(
                    {
                        "_id": existing["_id"],
                        "updated_at": existing.get("updated_at"),
                        "derived_at": existing.get("derived_at"),
                    },
                    {
                        # $set only the people/match entry via a dotted path (never
                        # the whole apollo_responses map) so a concurrent enrich /
                        # match writer's entry for another endpoint is not lost.
                        "$set": {
                            "entity_type": "person",
                            f"apollo_responses.{PERSON_MATCH}": new_match_entry,
                            "apollo_enriched": flags,
                            "embedding": False,
                            "updated_at": now,
                            "derived_at": now,
                            **derived,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
                if update_result.matched_count == 0:
                    continue
                doc = await db.leads.find_one({"_id": existing["_id"]})
                break
            try:
                insert_result = await db.leads.insert_one(
                    {
                        "apollo_id": apollo_id,
                        "entity_type": "person",
                        "apollo_responses": responses,
                        "apollo_enriched": flags,
                        "embedding": False,
                        "created_at": now,
                        "updated_at": now,
                        "derived_at": now,
                        **derived,
                    }
                )
            except DuplicateKeyError:
                continue
            doc = await db.leads.find_one({"_id": insert_result.inserted_id})
            break
        else:
            # Pathological contention: re-read to merge the freshest stored match
            # data, then a safe additive write only — the merged people/match entry
            # + updated_at, never derived fields/derived_at (nor apollo_enriched).
            existing = await db.leads.find_one({"apollo_id": apollo_id})
            if existing:
                responses = responses_from_doc(existing)
                prev = responses.get(PERSON_MATCH)
                data = (
                    dict(prev["data"])
                    if isinstance(prev, dict) and isinstance(prev.get("data"), dict)
                    else {}
                )
                data = _merge_async_person_into_match_data(data, entry, payload=payload)
                new_match_entry = endpoint_entry(data, now)
                await db.leads.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "entity_type": "person",
                            f"apollo_responses.{PERSON_MATCH}": new_match_entry,
                            "embedding": False,
                            "updated_at": now,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
                logger.warning(
                    "Optimistic guard exhausted for apollo_id %s (webhook); wrote "
                    "payload without re-deriving top-level fields",
                    apollo_id,
                )
                doc = await db.leads.find_one({"_id": existing["_id"]})
        if doc:
            indexed = await index_lead_docs([doc], source_precedence=EMBED_SOURCE_MATCH)
            if indexed:
                await _mark_embedded(indexed)
        updated_ids.append(apollo_id)

    return {"status": "ok", "updated": updated_ids, "count": len(updated_ids)}


# Batch size for hydrating similarity candidates from Mongo (matches the 500-doc
# batch convention used elsewhere, e.g. the search-side get_by_mongo_ids chunking).
_HYDRATE_CHUNK = 500


def _milvus_scalar_expr(
    company_filter_ids: list[str] | None,
    body: "SimilaritySearchRequest | GroupedSimilaritySearchRequest",
) -> str:
    """Milvus filter expr over the derived scalar fields (recall; re-checked in Mongo)."""
    parts: list[str] = []
    if company_filter_ids:
        if len(company_filter_ids) == 1:
            parts.append(f"company_id == {_milvus_str_literal(company_filter_ids[0])}")
        else:
            literals = ", ".join(_milvus_str_literal(v) for v in company_filter_ids)
            parts.append(f"company_id in [{literals}]")
    if body.entity_type is not None:
        parts.append(f"entity_type == {_milvus_str_literal(body.entity_type)}")
    if body.email_exists is not None:
        parts.append(f"has_email == {'true' if body.email_exists else 'false'}")
    if body.phone_exists is not None:
        parts.append(f"has_phone == {'true' if body.phone_exists else 'false'}")
    if body.linkedin_exists is not None:
        parts.append(f"has_linkedin == {'true' if body.linkedin_exists else 'false'}")
    return " and ".join(parts)


def _doc_passes_filters(
    doc: dict[str, Any],
    company_filter_ids: Collection[str] | None,
    body: "SimilaritySearchRequest | GroupedSimilaritySearchRequest",
) -> bool:
    """Authoritative re-check of every requested filter against the hydrated Mongo doc.

    Pass ``company_filter_ids`` as a set when calling per-doc in a loop —
    workflow-scale filters carry hundreds of ids and this runs per candidate.
    """
    if company_filter_ids:
        if str(doc.get("company_id") or "") not in company_filter_ids:
            return False
    if body.entity_type is not None:
        if (doc.get("entity_type") or "person") != body.entity_type:
            return False
    if body.email_exists is not None:
        if (doc.get("email") is not None) != body.email_exists:
            return False
    if body.phone_exists is not None:
        if (doc.get("phone") is not None) != body.phone_exists:
            return False
    if body.linkedin_exists is not None:
        if (doc.get("linkedin") is not None) != body.linkedin_exists:
            return False
    return True


async def _resolve_company_filter_ids(
    db: AsyncIOMotorDatabase,
    body: SimilaritySearchRequest,
) -> list[str] | None:
    """Resolve company_id + company_ids (union, order-preserving dedupe) to org _ids.

    Thin wrapper that builds the ordered value list from the single-search body and
    delegates to ``_resolve_company_values`` (shared with the grouped endpoint).
    """
    values: list[str] = []
    if body.company_id:
        values.append(body.company_id)
    for value in body.company_ids or []:
        if value not in values:
            values.append(value)
    if not values:
        return None
    return await _resolve_company_values(db, values) or None


async def _resolve_company_values(
    db: AsyncIOMotorDatabase,
    values: list[str],
) -> list[str]:
    """Resolve an ordered list of company id-space entries to org Mongo ``_id``s.

    People docs canonically store ``company_id`` = the organization DOCUMENT's
    Mongo ``_id`` (``company_apollo_id`` holds the raw Apollo org id as the
    resolution key). Each entry accepts either id space — a company record's Mongo
    ``_id`` or its Apollo organization id — and normalizes to the Mongo ``_id``.
    Order-preserving dedupe on the resolved ids. 404 when any entry misses both
    spaces.

    Batched: a prospect-run search passes EVERY resolved company (hundreds+), so
    resolution is two $in queries instead of a per-id find_one loop.
    """
    if not values:
        return []

    # Prefer the Mongo _id space (only for entries that parse as ObjectIds).
    object_ids: dict[str, ObjectId] = {}
    for value in values:
        try:
            object_ids[value] = ObjectId(value)
        except Exception:
            continue

    resolved_by_value: dict[str, str] = {}
    if object_ids:
        cursor = db.leads.find(
            {"_id": {"$in": list(object_ids.values())}},
            {"_id": 1, "entity_type": 1},
        )
        org_mongo_ids: set[str] = set()
        async for doc in cursor:
            if (doc.get("entity_type") or "person") == "organization":
                org_mongo_ids.add(str(doc["_id"]))
        for value, oid in object_ids.items():
            if str(oid) in org_mongo_ids:
                resolved_by_value[value] = str(oid)

    # Fall back to the Apollo organization id space for everything unresolved
    # (a non-ObjectId string is a legit Apollo org id, not a client error).
    unresolved = [v for v in values if v not in resolved_by_value]
    if unresolved:
        cursor = db.leads.find(
            {"apollo_id": {"$in": unresolved}, "entity_type": "organization"},
            {"_id": 1, "apollo_id": 1},
        )
        async for doc in cursor:
            apollo_id = str(doc.get("apollo_id") or "")
            if apollo_id and apollo_id not in resolved_by_value:
                resolved_by_value[apollo_id] = str(doc["_id"])

    missing = [v for v in values if v not in resolved_by_value]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No organization lead found for company_id {missing[0]} "
                "(accepts a company record's Mongo id or Apollo organization id)"
            ),
        )

    resolved: list[str] = []
    for value in values:
        rid = resolved_by_value[value]
        if rid not in resolved:
            resolved.append(rid)
    return resolved


async def _pure_filter_hits(
    db: AsyncIOMotorDatabase,
    *,
    company_filter_ids: list[str] | None,
    body: "SimilaritySearchRequest | GroupedSimilaritySearchRequest",
    limit: int,
) -> list[SimilarityHitOut]:
    """Straight Mongo filter search (no Milvus / OpenAI); score is null.

    Shared by the single similarity endpoint (``limit=body.limit``) and the grouped
    endpoint (one call per company, ``limit=per_company_limit``).
    """
    query: dict[str, Any] = {}
    if body.entity_type is not None:
        query["entity_type"] = body.entity_type
    if company_filter_ids:
        query["company_id"] = (
            company_filter_ids[0]
            if len(company_filter_ids) == 1
            else {"$in": company_filter_ids}
        )
        # company_id is a people-only field; default to person unless the caller
        # already pinned an entity_type (an incompatible pin ANDs to no results,
        # consistent with the vector path).
        query.setdefault("entity_type", "person")
    for field, flag in (
        ("email", body.email_exists),
        ("phone", body.phone_exists),
        ("linkedin", body.linkedin_exists),
    ):
        if flag is True:
            query[field] = {"$ne": None}
        elif flag is False:
            query[field] = None  # matches missing or null
    cursor = db.leads.find(query).sort("updated_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [
        SimilarityHitOut(score=None, lead=_serialize_lead(doc, body.fields)) for doc in docs
    ]


async def _pure_filter_similarity(
    db: AsyncIOMotorDatabase,
    body: SimilaritySearchRequest,
    company_filter_ids: list[str] | None,
) -> SimilaritySearchResponse:
    """Straight Mongo filter search (no Milvus / OpenAI); score is null."""
    hits = await _pure_filter_hits(
        db, company_filter_ids=company_filter_ids, body=body, limit=body.limit
    )
    return SimilaritySearchResponse(results=hits)


async def _vector_ranked_hits(
    db: AsyncIOMotorDatabase,
    *,
    query_vector: Any,
    embeds: list[str],
    company_filter_ids: list[str] | None,
    company_filter_id_set: set[str] | None,
    body: "SimilaritySearchRequest | GroupedSimilaritySearchRequest",
    limit: int,
    settings: Settings,
    nice: int = NICE_INTERACTIVE,
) -> list[SimilarityHitOut]:
    """Rank leads by mean similarity across the selected embed kinds, filtered.

    The shared core of the vector path: one ANN search per embed kind reusing the
    single pre-computed ``query_vector``, mean-score merge, then an authoritative
    Mongo re-check while filling to ``limit``. Reused by the single endpoint
    (``limit=body.limit``, default ``NICE_INTERACTIVE``) and per-company by the
    grouped endpoint (``limit=per_company_limit``, ``nice=NICE_BULK_READ`` so one
    lone user query outranks the fan-out). ``search_similar`` may raise (Milvus
    down) — the caller maps that to a sanitized 503.
    """
    scalar_expr = _milvus_scalar_expr(company_filter_ids, body)
    # Split the ANN candidate budget across the selected kinds. Total candidate
    # work is bounded by max(16384, limit * len(embeds)): the per-kind floor of
    # `limit` is deliberate (each kind must be able to contribute a full result
    # set). Each kind is one ANN search on the serializing Milvus gate.
    oversample = min(limit * 4, max(limit, 16384 // len(embeds)))
    # score contributions per doc, keyed by the kinds it appears in.
    per_doc_scores: dict[str, dict[str, float]] = {}
    for kind in embeds:
        expr = f"embed_kind == {_milvus_str_literal(kind)}"
        if scalar_expr:
            expr = f"{expr} and {scalar_expr}"
        hits = await search_similar(
            query_vector, expr=expr, limit=oversample, nice=nice, settings=settings
        )
        for mongo_id, score in hits:
            per_doc_scores.setdefault(mongo_id, {})[kind] = score

    # Mean similarity over the selected kinds each doc actually has (no zero-penalty).
    merged: list[tuple[str, float]] = []
    for mongo_id, kind_scores in per_doc_scores.items():
        if not kind_scores:
            continue
        merged.append((mongo_id, sum(kind_scores.values()) / len(kind_scores)))
    merged.sort(key=lambda item: item[1], reverse=True)

    # Fill to ``limit`` AFTER the authoritative Mongo re-check: hydrate the merged
    # candidates in score order, chunked (the 500 convention), applying the filters
    # per doc and accumulating passing hits until ``limit`` is reached or candidates
    # are exhausted. Bound the scan to cap worst-case sequential Mongo work; when the
    # cap truncates before ``limit`` accumulates we log (no silent caps).
    hydration_cap = max(limit * 10, 2000)
    candidates = merged[:hydration_cap]
    results: list[SimilarityHitOut] = []
    for chunk_start in range(0, len(candidates), _HYDRATE_CHUNK):
        if len(results) >= limit:
            break
        chunk = candidates[chunk_start : chunk_start + _HYDRATE_CHUNK]
        object_ids: list[ObjectId] = []
        for mongo_id, _score in chunk:
            try:
                object_ids.append(ObjectId(mongo_id))
            except Exception:
                continue
        if not object_ids:
            continue
        docs_by_id: dict[str, dict[str, Any]] = {}
        cursor = db.leads.find({"_id": {"$in": object_ids}})
        async for doc in cursor:
            docs_by_id[str(doc["_id"])] = doc
        # Preserve merged score order within the chunk.
        for mongo_id, score in chunk:
            doc = docs_by_id.get(mongo_id)
            if not doc:
                continue
            # Authoritative re-check against Mongo (defends against Milvus scalar staleness).
            if not _doc_passes_filters(doc, company_filter_id_set, body):
                continue
            results.append(SimilarityHitOut(score=score, lead=_serialize_lead(doc, body.fields)))
            if len(results) >= limit:
                break
    if len(results) < limit and len(merged) > hydration_cap:
        logger.warning(
            "vector_ranked_hits hydration cap reached: examined %s of %s merged "
            "candidate(s), returning %s of requested %s result(s)",
            len(candidates),
            len(merged),
            len(results),
            limit,
        )
    return results


@router.post("/similarity-search", response_model=SimilaritySearchResponse)
async def similarity_search(
    body: SimilaritySearchRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> SimilaritySearchResponse:
    """Rank leads by mean similarity across a selected subset of embed kinds, with filters.

    ``embeds`` (omitted => ``["apollo"]``; ``[]`` => pure filter search) selects the
    per-kind embeddings to average; ``company_id`` / ``email_exists`` /
    ``phone_exists`` / ``linkedin_exists`` filter the result set. Milvus scalar
    filters provide recall; the hydrated Mongo docs are re-checked authoritatively.
    """
    company_filter_ids = await _resolve_company_filter_ids(db, body)
    # Set for the per-candidate re-check loop; the ordered list still feeds the
    # Milvus expr builder.
    company_filter_id_set = set(company_filter_ids) if company_filter_ids else None
    embeds = body.embeds if body.embeds is not None else ["apollo"]

    # Pure filter search: no Milvus / OpenAI (works even when either is down).
    if not embeds:
        return await _pure_filter_similarity(db, body, company_filter_ids)

    if not settings.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    query = (body.query or "").strip()

    try:
        vectors = await embed_texts([query], settings=settings)
        query_vector = vectors[0]
        results = await _vector_ranked_hits(
            db,
            query_vector=query_vector,
            embeds=embeds,
            company_filter_ids=company_filter_ids,
            company_filter_id_set=company_filter_id_set,
            body=body,
            limit=body.limit,
            settings=settings,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similarity search failed")
        # Sanitized code only — this 503 detail is forwarded verbatim into the
        # browser-facing (and MCP-tool) error body by search, so it must not carry
        # the raw Milvus exception text (URIs / quota internals). Full exc logged above.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"similarity_unavailable:{classify_write_error(exc)}",
        ) from exc

    return SimilaritySearchResponse(results=results)


# Per-company fan-out concurrency (fallback path). Each company runs a vector
# search on the serializing Milvus gate; bound in-flight work.
_GROUPED_FANOUT_CONCURRENCY = 8
# P4 cost guards (both re-checked on the RESOLVED company count in the router; the
# ANN one is also pre-checked in the schema on the raw company_ids count). These
# bound the FALLBACK fan-out; the grouping path is only a handful of calls but the
# caps still gate the request size uniformly.
_GROUPED_MAX_TOTAL_HITS = 5000  # resolved companies * per_company_limit
_GROUPED_MAX_ANN_CALLS = 3000  # resolved companies * max(1, embed kinds)

# Milvus grouping-search sizing. group_size overfetches per company (headroom for
# the authoritative Mongo re-check). companies_per_chunk is bounded so
# limit(=companies) * group_size stays comfortably under Milvus's grouping product
# ceiling (16384). Total grouping calls = kinds * ceil(companies / chunk).
_GROUPING_MAX_GROUP_SIZE = 100
_GROUPING_MAX_COMPANIES_PER_CALL = 800
_GROUPING_LIMIT_GROUPSIZE_PRODUCT = 12000


class _ClientDisconnected(Exception):
    """Raised by a per-company worker when the caller has hung up mid-fan-out.

    A normal exception (not ``CancelledError``, whose TaskGroup semantics are
    ambiguous) so the TaskGroup deterministically cancels the sibling workers.
    """


class _GroupingUnsupported(Exception):
    """The Milvus server rejected grouping search — signals a fallback to fan-out."""


# ONE process-wide fan-out semaphore (not per-request): a per-request Semaphore(8)
# would let N concurrent grouped requests run 8N searches at once. Lazily bound to
# the running loop (recreated on a new loop, e.g. tests), mirroring the _gate() /
# _embed_sem() pattern.
_grouped_fanout_sem: asyncio.Semaphore | None = None
_grouped_fanout_sem_loop: asyncio.AbstractEventLoop | None = None


def _grouped_fanout_semaphore() -> asyncio.Semaphore:
    global _grouped_fanout_sem, _grouped_fanout_sem_loop
    loop = asyncio.get_running_loop()
    if _grouped_fanout_sem is None or _grouped_fanout_sem_loop is not loop:
        _grouped_fanout_sem = asyncio.Semaphore(_GROUPED_FANOUT_CONCURRENCY)
        _grouped_fanout_sem_loop = loop
    return _grouped_fanout_sem


def _grouping_plan(
    num_companies: int, num_kinds: int, per_company_limit: int
) -> tuple[int, int, int]:
    """Return ``(group_size, companies_per_chunk, total_calls)`` for a grouping run.

    ``group_size`` overfetches ~3x per_company_limit (re-check headroom, capped).
    ``companies_per_chunk`` keeps ``companies * group_size`` under the product
    ceiling. ``total_calls = kinds * ceil(companies / chunk)`` — single digits even
    at thousands of companies.
    """
    group_size = min(_GROUPING_MAX_GROUP_SIZE, max(1, per_company_limit) * 3)
    companies_per_chunk = max(
        1,
        min(_GROUPING_MAX_COMPANIES_PER_CALL, _GROUPING_LIMIT_GROUPSIZE_PRODUCT // group_size),
    )
    num_chunks = (
        (num_companies + companies_per_chunk - 1) // companies_per_chunk
        if num_companies
        else 0
    )
    return group_size, companies_per_chunk, max(1, num_kinds) * num_chunks


async def _grouped_via_grouping_search(
    db: AsyncIOMotorDatabase,
    *,
    resolved: list[str],
    embeds: list[str],
    query_vector: Any,
    body: GroupedSimilaritySearchRequest,
    settings: Settings,
) -> list[list[SimilarityHitOut]]:
    """Per-company top hits via Milvus GROUPING search — ``kinds * ceil(companies/chunk)``
    calls instead of ``companies * kinds``.

    Per embed kind, one grouping search per company chunk (group_by company_id,
    overfetching ``group_size`` per company). Scores are merged per doc (mean across
    the kinds that returned it), candidates are batch-hydrated across ALL companies,
    then each company's candidates get the authoritative Mongo re-check and the top
    ``per_company_limit`` survivors. If re-check drops a company below
    ``per_company_limit`` we return what survived (no per-company top-up fan-out).

    Raises ``_GroupingUnsupported`` if the FIRST grouping call is rejected as a
    grouping capability/param error (caller falls back to the fan-out); any other
    error propagates (caller maps to a sanitized 503).
    """
    per_company_limit = body.per_company_limit
    group_size, companies_per_chunk, _ = _grouping_plan(
        len(resolved), len(embeds), per_company_limit
    )

    per_doc_scores: dict[str, dict[str, float]] = {}
    candidate_company: dict[str, str] = {}
    first_call = True
    for kind in embeds:
        kind_expr = f"embed_kind == {_milvus_str_literal(kind)}"
        for chunk_start in range(0, len(resolved), companies_per_chunk):
            chunk = resolved[chunk_start : chunk_start + companies_per_chunk]
            # company_id in [chunk] + the other scalar filters (never the embed_kind).
            scalar = _milvus_scalar_expr(chunk, body)
            expr = f"{kind_expr} and {scalar}" if scalar else kind_expr
            try:
                hits = await search_similar_grouped(
                    query_vector,
                    expr=expr,
                    group_by_field="company_id",
                    group_size=group_size,
                    limit=len(chunk),
                    nice=NICE_BULK_READ,
                    settings=settings,
                )
            except Exception as exc:
                if first_call and is_grouping_unsupported(exc):
                    raise _GroupingUnsupported() from exc
                raise
            first_call = False
            for mongo_id, company_id, score in hits:
                per_doc_scores.setdefault(mongo_id, {})[kind] = score
                candidate_company[mongo_id] = company_id

    # Batch-hydrate ALL candidate docs across companies (500-chunks; NOT per-company).
    all_ids = list(per_doc_scores.keys())
    docs_by_id: dict[str, dict[str, Any]] = {}
    for chunk_start in range(0, len(all_ids), _HYDRATE_CHUNK):
        object_ids: list[ObjectId] = []
        for mongo_id in all_ids[chunk_start : chunk_start + _HYDRATE_CHUNK]:
            try:
                object_ids.append(ObjectId(mongo_id))
            except Exception:
                continue
        if not object_ids:
            continue
        cursor = db.leads.find({"_id": {"$in": object_ids}})
        async for doc in cursor:
            docs_by_id[str(doc["_id"])] = doc

    # Bucket candidates by their (Milvus-reported) company; mean score across kinds.
    merged_by_company: dict[str, list[tuple[str, float]]] = {}
    for mongo_id, kind_scores in per_doc_scores.items():
        company_id = candidate_company.get(mongo_id)
        if not company_id:
            continue
        mean = sum(kind_scores.values()) / len(kind_scores)
        merged_by_company.setdefault(company_id, []).append((mongo_id, mean))
    for candidates in merged_by_company.values():
        candidates.sort(key=lambda item: item[1], reverse=True)

    # Per company (request order): authoritative Mongo re-check + top per_company_limit.
    # The re-check against ``{org_id}`` drops a doc whose stored company_id drifted
    # from the grouped value (defends against Milvus scalar staleness).
    hits_per_company: list[list[SimilarityHitOut]] = []
    for org_id in resolved:
        company_hits: list[SimilarityHitOut] = []
        for mongo_id, score in merged_by_company.get(org_id, []):
            doc = docs_by_id.get(mongo_id)
            if not doc:
                continue
            if not _doc_passes_filters(doc, {org_id}, body):
                continue
            company_hits.append(
                SimilarityHitOut(score=score, lead=_serialize_lead(doc, body.fields))
            )
            if len(company_hits) >= per_company_limit:
                break
        hits_per_company.append(company_hits)
    return hits_per_company


async def _grouped_via_fanout(
    db: AsyncIOMotorDatabase,
    *,
    request: Request,
    resolved: list[str],
    embeds: list[str],
    query_vector: Any,
    body: GroupedSimilaritySearchRequest,
    settings: Settings,
) -> list[list[SimilarityHitOut]] | None:
    """Per-company fan-out (the pure-filter path + the grouping-unsupported fallback).

    One bounded, structured (TaskGroup) task per company reusing the single-search
    internals; first failure cancels every sibling (no orphans). Returns
    ``None`` when the caller disconnected; raises a sanitized 503 on Milvus failure.
    """
    hits_per_company: list[list[SimilarityHitOut]] = [[] for _ in resolved]
    sem = _grouped_fanout_semaphore()

    async def _hits_for_company(org_id: str) -> list[SimilarityHitOut]:
        if not embeds:
            return await _pure_filter_hits(
                db, company_filter_ids=[org_id], body=body, limit=body.per_company_limit
            )
        return await _vector_ranked_hits(
            db,
            query_vector=query_vector,
            embeds=embeds,
            company_filter_ids=[org_id],
            company_filter_id_set={org_id},
            body=body,
            limit=body.per_company_limit,
            settings=settings,
            nice=NICE_BULK_READ,
        )

    async def _worker(idx: int, org_id: str) -> None:
        async with sem:
            # Cheap abandoned-caller check before spending work; bailing cancels siblings.
            if await request.is_disconnected():
                raise _ClientDisconnected()
            hits_per_company[idx] = await _hits_for_company(org_id)

    disconnected = False
    try:
        async with asyncio.TaskGroup() as tg:
            for idx, oid in enumerate(resolved):
                tg.create_task(_worker(idx, oid))
    except* _ClientDisconnected:
        disconnected = True
    except* Exception as eg:
        logger.exception("Grouped similarity fan-out failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"similarity_unavailable:{classify_write_error(eg.exceptions[0])}",
        ) from eg
    # ``return`` is not permitted inside an except* block — decide out here.
    return None if disconnected else hits_per_company


@router.post("/similarity-search-grouped", response_model=GroupedSimilaritySearchResponse)
async def similarity_search_grouped(
    body: GroupedSimilaritySearchRequest,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> GroupedSimilaritySearchResponse:
    """Per-company top-``per_company_limit`` semantic search over a required company set.

    Returns one group per company (in request order, post-dedupe), each with the org
    lead doc + its top hits; companies with zero hits still appear with ``hits: []``.
    The query is embedded ONCE and reused across companies.

    Vector mode uses ONE Milvus GROUPING search per embed kind per company-chunk
    (``kinds * ceil(companies/chunk)`` calls — see ``_grouped_via_grouping_search``)
    so a 900-company run is a handful of gate ops, not ~2700. If the server rejects
    grouping it transparently falls back to the per-company fan-out
    (``_grouped_via_fanout``); pure-filter mode always uses the fan-out (Mongo-only).
    Every read runs at ``NICE_BULK_READ`` so a lone interactive query wins the gate.

    Cost guards: the schema pre-rejects ``company_ids * max(1, embeds) > 3000``; THIS
    router re-checks that on the RESOLVED company count and enforces the hit budget
    ``resolved * per_company_limit <= 5000`` (both 422; they gate the fallback fan-out
    and cap request size uniformly).
    """
    resolved = await _resolve_company_values(db, body.company_ids)
    embeds = body.embeds if body.embeds is not None else ["apollo"]
    if len(resolved) * body.per_company_limit > _GROUPED_MAX_TOTAL_HITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"resolved company count ({len(resolved)}) * per_company_limit "
                f"({body.per_company_limit}) exceeds the {_GROUPED_MAX_TOTAL_HITS} cap"
            ),
        )
    if len(resolved) * max(1, len(embeds)) > _GROUPED_MAX_ANN_CALLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"resolved company count ({len(resolved)}) * embed kinds "
                f"({max(1, len(embeds))}) exceeds the {_GROUPED_MAX_ANN_CALLS} "
                "ANN-call budget"
            ),
        )

    # Batch-hydrate the org docs (each group's ``company`` field) across all
    # companies in 500-doc chunks — one cross-company batch.
    company_docs: dict[str, dict[str, Any]] = {}
    resolved_object_ids = [ObjectId(rid) for rid in resolved]
    for chunk_start in range(0, len(resolved_object_ids), _HYDRATE_CHUNK):
        chunk = resolved_object_ids[chunk_start : chunk_start + _HYDRATE_CHUNK]
        cursor = db.leads.find({"_id": {"$in": chunk}})
        async for doc in cursor:
            company_docs[str(doc["_id"])] = doc

    if not embeds:
        # Pure-filter (Mongo-only): grouping search does not apply — use the fan-out.
        fanout = await _grouped_via_fanout(
            db, request=request, resolved=resolved, embeds=[], query_vector=None,
            body=body, settings=settings,
        )
        if fanout is None:
            return GroupedSimilaritySearchResponse(groups=[], total=0)
        hits_per_company = fanout
    else:
        if not settings.openai_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OPENAI_API_KEY is not configured",
            )
        # Embed the query ONCE for the whole request; reused for every company/kind.
        query = (body.query or "").strip()
        try:
            vectors = await embed_texts([query], settings=settings)
        except Exception as exc:
            logger.exception("Grouped similarity embed failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"similarity_unavailable:{classify_write_error(exc)}",
            ) from exc
        query_vector = vectors[0]

        try:
            hits_per_company = await _grouped_via_grouping_search(
                db, resolved=resolved, embeds=embeds, query_vector=query_vector,
                body=body, settings=settings,
            )
        except _GroupingUnsupported:
            logger.warning(
                "Milvus grouping search unsupported; falling back to per-company fan-out"
            )
            fanout = await _grouped_via_fanout(
                db, request=request, resolved=resolved, embeds=embeds,
                query_vector=query_vector, body=body, settings=settings,
            )
            if fanout is None:
                return GroupedSimilaritySearchResponse(groups=[], total=0)
            hits_per_company = fanout
        except Exception as exc:
            logger.exception("Grouped similarity (grouping) search failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"similarity_unavailable:{classify_write_error(exc)}",
            ) from exc

    groups: list[SimilarityGroupOut] = []
    total = 0
    for org_id, hits in zip(resolved, hits_per_company, strict=True):
        company_doc = company_docs.get(org_id)
        if company_doc is None:
            # Resolution just confirmed this org exists; a miss here is a rare race.
            logger.warning("Grouped similarity: company doc %s vanished post-resolution", org_id)
            continue
        total += len(hits)
        groups.append(
            SimilarityGroupOut(
                company_id=org_id,
                company=_serialize_lead(company_doc, body.fields),
                hits=hits,
            )
        )
    return GroupedSimilaritySearchResponse(groups=groups, total=total)


@router.post("", response_model=list[LeadOut])
@router.post("/", response_model=list[LeadOut], include_in_schema=False)
async def get_leads_by_mongo_ids(
    body: BatchMongoIdsRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> list[LeadOut]:
    """Return leads for the given Mongo `_id`s, preserving request order.

    Missing ids are omitted. Used by the search backend to hydrate history pages.
    """
    ordered_ids: list[str] = []
    seen: set[str] = set()
    object_ids: list[ObjectId] = []
    for raw in body.ids:
        mongo_id = str(raw or "").strip()
        if not mongo_id or mongo_id in seen:
            continue
        try:
            object_id = ObjectId(mongo_id)
        except Exception:
            continue
        seen.add(mongo_id)
        ordered_ids.append(mongo_id)
        object_ids.append(object_id)
    if not ordered_ids:
        return []

    cursor = db.leads.find({"_id": {"$in": object_ids}})
    by_id = {str(doc["_id"]): doc async for doc in cursor}
    return [
        _serialize_lead(by_id[mongo_id], body.fields)
        for mongo_id in ordered_ids
        if mongo_id in by_id
    ]
