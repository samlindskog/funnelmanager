import asyncio
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import unquote

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fm_runtime import anonymous, confirmation_threshold, require_confirmation
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.apollo import ApolloLeadsClient
from app.apollo_endpoints import (
    ORG_BY_ID,
    ORG_SEARCH,
    PERSON_BY_ID,
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
    NICE_SEARCH_EMBED,
    _milvus_str_literal,
    index_lead_docs,
    search_similar,
)
from app.schemas import (
    ApolloEndpointResponseOut,
    ApolloEnrichedFlags,
    ApolloParamsBody,
    BatchMongoIdsRequest,
    EmbeddingBackfillResponse,
    LeadOut,
    SearchIdsOut,
    SimilarityHitOut,
    SimilaritySearchRequest,
    SimilaritySearchResponse,
    StreamControlResponse,
    StreamSubscribeRequest,
)
from app.stream_jobs import (
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


def _serialize_lead(doc: dict[str, Any]) -> LeadOut:
    responses = responses_from_doc(doc)
    flags = normalize_apollo_enriched(doc, responses=responses)
    return LeadOut(
        id=str(doc["_id"]),
        apollo_id=str(doc["apollo_id"]),
        entity_type=doc.get("entity_type") or "person",
        embedding=bool(doc.get("embedding")),
        apollo_enriched=ApolloEnrichedFlags(**flags),
        apollo_responses=_serialize_responses(responses),
        name=doc.get("name"),
        title=doc.get("title"),
        company_id=doc.get("company_id"),
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
) -> list[str]:
    """Embed leads (respecting precedence) and record source. Returns indexed mongo ids.

    ``force`` re-embeds even docs that already carry a vector (backfill); see
    ``index_lead_docs``. ``nice`` sets the Milvus-gate priority of the upsert
    (live search passes ``NICE_SEARCH_EMBED``; backfill passes ``NICE_BACKFILL``).
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
        docs, source_precedence=source_precedence, force=force, nice=nice
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
    It fills ``company_id`` ONLY when neither the payload-derived fields nor
    the stored doc already carry one — enrichment-derived values always win.
    """
    now = datetime.now(timezone.utc)
    mongo_ids: list[str] = []

    def _apply_company_fallback(derived: dict[str, Any], existing_doc: dict[str, Any] | None) -> None:
        if (
            fallback_company_id
            and entity_type == "person"
            and "company_id" not in derived
            and not (existing_doc or {}).get("company_id")
        ):
            derived["company_id"] = fallback_company_id

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
                    **derive_top_fields(entity_type, {endpoint: entry}),
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
    return json.dumps(payload, default=str) + "\n"


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
                            "detail": f"{apollo_id}: {exc}",
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
            await manager.publish(
                ingest_stream_id,
                {"type": "error", "kind": "ingest", "detail": str(exc)},
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.ERROR, error=str(exc))
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
                            "detail": f"{apollo_id}: {exc}",
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
            await manager.publish(
                ingest_stream_id,
                {"type": "error", "kind": "ingest", "detail": str(exc)},
            )
            await manager.finish(ingest_stream_id, status=StreamJobStatus.ERROR, error=str(exc))
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


def _milvus_scalar_expr(company_apollo_id: str | None, body: SimilaritySearchRequest) -> str:
    """Milvus filter expr over the derived scalar fields (recall; re-checked in Mongo)."""
    parts: list[str] = []
    if company_apollo_id:
        parts.append(f"company_id == {_milvus_str_literal(company_apollo_id)}")
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
    company_apollo_id: str | None,
    body: SimilaritySearchRequest,
) -> bool:
    """Authoritative re-check of every requested filter against the hydrated Mongo doc."""
    if company_apollo_id is not None:
        if str(doc.get("company_id") or "") != company_apollo_id:
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


async def _resolve_company_apollo_id(
    db: AsyncIOMotorDatabase,
    company_id: str | None,
) -> str | None:
    """Resolve ``company_id`` to the org's Apollo id (the people filter value).

    Accepts either a company record's Mongo ``_id`` or its Apollo organization id
    (the two live in different id spaces, so a caller cannot know which they hold).
    Tries the Mongo ``_id`` first (when the value parses as an ObjectId); on a miss
    (or a non-ObjectId value) falls back to the org doc's ``apollo_id``. 404 only
    when both lookups miss. The resolved org's ``apollo_id`` is the filter value.
    """
    if company_id is None:
        return None
    value = company_id.strip()
    if not value:
        return None

    org_doc: dict[str, Any] | None = None
    # Prefer the Mongo _id space (only when the value is a valid ObjectId).
    try:
        org_object_id = ObjectId(value)
    except Exception:
        org_object_id = None
    if org_object_id is not None:
        candidate = await db.leads.find_one({"_id": org_object_id})
        if candidate and (candidate.get("entity_type") or "person") == "organization":
            org_doc = candidate

    # Fall back to the Apollo organization id (a non-ObjectId string is a legit
    # Apollo org id, not a client error).
    if org_doc is None:
        org_doc = await db.leads.find_one(
            {"apollo_id": value, "entity_type": "organization"}
        )

    if org_doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No organization lead found for company_id {company_id} "
                "(accepts a company record's Mongo id or Apollo organization id)"
            ),
        )
    apollo_id = str(org_doc.get("apollo_id") or "").strip()
    if not apollo_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization lead {company_id} has no Apollo id to filter by",
        )
    return apollo_id


async def _pure_filter_similarity(
    db: AsyncIOMotorDatabase,
    body: SimilaritySearchRequest,
    company_apollo_id: str | None,
) -> SimilaritySearchResponse:
    """Straight Mongo filter search (no Milvus / OpenAI); score is null."""
    query: dict[str, Any] = {}
    if body.entity_type is not None:
        query["entity_type"] = body.entity_type
    if company_apollo_id is not None:
        query["company_id"] = company_apollo_id
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
    cursor = db.leads.find(query).sort("updated_at", -1).limit(body.limit)
    docs = await cursor.to_list(length=body.limit)
    results = [SimilarityHitOut(score=None, lead=_serialize_lead(doc)) for doc in docs]
    return SimilaritySearchResponse(results=results)


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
    company_apollo_id = await _resolve_company_apollo_id(db, body.company_id)
    embeds = body.embeds if body.embeds is not None else ["apollo"]

    # Pure filter search: no Milvus / OpenAI (works even when either is down).
    if not embeds:
        return await _pure_filter_similarity(db, body, company_apollo_id)

    if not settings.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    query = (body.query or "").strip()

    try:
        vectors = await embed_texts([query], settings=settings)
        query_vector = vectors[0]
        scalar_expr = _milvus_scalar_expr(company_apollo_id, body)
        # Split the ANN candidate budget across the selected kinds. Total candidate
        # work is bounded by max(16384, limit * len(embeds)): the per-kind floor of
        # `limit` is deliberate (each kind must be able to contribute a full result
        # set), so a large limit CAN exceed 16384 (e.g. limit=10000 x 3 kinds =
        # 30000) — but it never N-multiplies the 16384 ceiling the way an unsplit
        # min(limit*4, 16384) per kind would. This endpoint is agent-reachable and
        # each kind is one ANN search on the serializing Milvus gate (P4 amplification).
        oversample = min(body.limit * 4, max(body.limit, 16384 // len(embeds)))
        # score contributions per doc, keyed by the kinds it appears in.
        per_doc_scores: dict[str, dict[str, float]] = {}
        for kind in embeds:
            expr = f"embed_kind == {_milvus_str_literal(kind)}"
            if scalar_expr:
                expr = f"{expr} and {scalar_expr}"
            hits = await search_similar(
                query_vector, expr=expr, limit=oversample, settings=settings
            )
            for mongo_id, score in hits:
                per_doc_scores.setdefault(mongo_id, {})[kind] = score
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similarity search failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Similarity search unavailable: {exc}",
        ) from exc

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
    # are exhausted. Truncating to ``limit`` before the re-check could return fewer
    # than ``limit`` rows when the head of the list carries stale Milvus scalars.
    # Bound the fill-to-limit hydration scan: examine at most this many merged
    # candidates (each hydrated from Mongo in 500-doc chunks). Caps the worst-case
    # sequential Mongo work on this agent-reachable endpoint; when the cap
    # truncates before ``limit`` results accumulate we log (no silent caps) and
    # return what passed.
    hydration_cap = max(body.limit * 10, 2000)
    candidates = merged[:hydration_cap]
    results: list[SimilarityHitOut] = []
    for chunk_start in range(0, len(candidates), _HYDRATE_CHUNK):
        if len(results) >= body.limit:
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
            if not _doc_passes_filters(doc, company_apollo_id, body):
                continue
            results.append(SimilarityHitOut(score=score, lead=_serialize_lead(doc)))
            if len(results) >= body.limit:
                break
    if len(results) < body.limit and len(merged) > hydration_cap:
        logger.warning(
            "similarity_search hydration cap reached: examined %s of %s merged "
            "candidate(s), returning %s of requested %s result(s)",
            len(candidates),
            len(merged),
            len(results),
            body.limit,
        )
    return SimilaritySearchResponse(results=results)


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
    return [_serialize_lead(by_id[mongo_id]) for mongo_id in ordered_ids if mongo_id in by_id]
