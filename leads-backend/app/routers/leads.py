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
from app.embeddings import (
    EMBED_SOURCE_COMPLETE_INFO,
    EMBED_SOURCE_MATCH,
    EMBED_SOURCE_SEARCH,
    embed_texts,
    endpoint_source_precedence,
)
from app.milvus_client import index_lead_docs, search_similar
from app.schemas import (
    ApolloEndpointResponseOut,
    ApolloEnrichedFlags,
    ApolloParamsBody,
    BatchMongoIdsRequest,
    LeadOut,
    SearchIdsOut,
    SimilarityHitOut,
    SimilaritySearchRequest,
    SimilaritySearchResponse,
    StreamSubscribeRequest,
)
from app.stream_jobs import (
    cancel_stream,
    close_embedding_stream,
    create_embedding_stream,
    iter_stream_events,
    run_paged_search_with_embedding,
    schedule_embedding_batch,
    stream_job_manager,
)

logger = logging.getLogger(__name__)

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
) -> list[str]:
    """Embed leads (respecting precedence) and record source. Returns indexed mongo ids."""
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
    indexed = await index_lead_docs(docs, source_precedence=source_precedence)
    if indexed:
        await _mark_embedded(indexed)
    return [mongo_id for mongo_id, _ in indexed]


async def _background_embed_mongo_ids(mongo_ids: list[str], source_precedence: int) -> None:
    """Embed leads in the background (no stream progress)."""
    if not mongo_ids:
        return
    try:
        await _embed_mongo_ids_batch(mongo_ids, source_precedence=source_precedence)
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
) -> list[str]:
    """Merge each search hit into apollo_responses[endpoint]; return mongo `_id`s in hit order."""
    now = datetime.now(timezone.utc)
    mongo_ids: list[str] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        apollo_id = id_getter(record)
        if not apollo_id:
            continue

        entry = endpoint_entry(record, now)
        # Two attempts: a concurrent stream may insert the same apollo_id between
        # our find and insert (unique index); retry lands on the update path.
        for _attempt in range(2):
            existing = await db.leads.find_one({"apollo_id": apollo_id})
            if existing:
                responses = responses_from_doc(existing)
                responses[endpoint] = entry
                await db.leads.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "entity_type": entity_type,
                            "apollo_responses": responses,
                            "embedding": _embedding_flag_after_update(existing, endpoint),
                            "updated_at": now,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
                mongo_ids.append(str(existing["_id"]))
                break

            doc = {
                "apollo_id": apollo_id,
                "entity_type": entity_type,
                "apollo_responses": {endpoint: entry},
                "apollo_enriched": empty_enriched_flags(),
                "embedding": False,
                "created_at": now,
                "updated_at": now,
            }
            try:
                result = await db.leads.insert_one(doc)
            except DuplicateKeyError:
                continue
            mongo_ids.append(str(result.inserted_id))
            break

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
    # Two attempts: a concurrent stream may insert the same apollo_id between
    # our find and insert (unique index); retry lands on the update path.
    for _attempt in range(2):
        existing = await db.leads.find_one({"apollo_id": apollo_id})
        if existing:
            responses = responses_from_doc(existing)
            responses[endpoint] = entry
            flags = merge_enriched_flags(
                normalize_apollo_enriched(existing, responses=responses),
                linkedin=linkedin,
                email=email,
                phone=phone,
            )
            await db.leads.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "entity_type": entity_type,
                        "apollo_responses": responses,
                        "apollo_enriched": flags,
                        "embedding": _embedding_flag_after_update(existing, endpoint),
                        "updated_at": now,
                    },
                    "$unset": {"apollo_response": ""},
                },
            )
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
                }
            )
        except DuplicateKeyError:
            continue
        doc = await db.leads.find_one({"_id": result.inserted_id})
        break

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
async def leads_health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "leads",
        "apollo_configured": settings.apollo_configured,
        "openai_configured": settings.openai_configured,
        "milvus_uri": settings.milvus_uri,
        "milvus_collection": settings.milvus_collection,
    }


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
    mongo_ids = await _upsert_search_records(
        database,
        entity_type="person",
        records=people,
        id_getter=_person_id_from_record,
        endpoint=PERSON_SEARCH,
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
        embed_batch=lambda ids: _embed_mongo_ids_batch(ids, source_precedence=EMBED_SOURCE_SEARCH),
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
        embed_batch=lambda ids: _embed_mongo_ids_batch(ids, source_precedence=EMBED_SOURCE_SEARCH),
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
                        ids, source_precedence=EMBED_SOURCE_COMPLETE_INFO
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
                        ids, source_precedence=EMBED_SOURCE_MATCH
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
        # Two attempts: a concurrent upsert may insert the same apollo_id between
        # our find and insert (unique index); retry lands on the update path.
        for _attempt in range(2):
            existing = await db.leads.find_one({"apollo_id": apollo_id})
            responses = responses_from_doc(existing) if existing else {}
            match_entry = responses.get(PERSON_MATCH)
            if isinstance(match_entry, dict) and isinstance(match_entry.get("data"), dict):
                data = dict(match_entry["data"])
            else:
                data = {}

            data = _merge_async_person_into_match_data(data, entry, payload=payload)
            responses[PERSON_MATCH] = endpoint_entry(data, now)
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
            if existing:
                await db.leads.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "entity_type": "person",
                            "apollo_responses": responses,
                            "apollo_enriched": flags,
                            "embedding": False,
                            "updated_at": now,
                        },
                        "$unset": {"apollo_response": ""},
                    },
                )
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
                    }
                )
            except DuplicateKeyError:
                continue
            doc = await db.leads.find_one({"_id": insert_result.inserted_id})
            break
        if doc:
            indexed = await index_lead_docs([doc], source_precedence=EMBED_SOURCE_MATCH)
            if indexed:
                await _mark_embedded(indexed)
        updated_ids.append(apollo_id)

    return {"status": "ok", "updated": updated_ids, "count": len(updated_ids)}


@router.post("/similarity-search", response_model=SimilaritySearchResponse)
async def similarity_search(
    body: SimilaritySearchRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> SimilaritySearchResponse:
    """Embed `query` and return top similar leads from Milvus + Mongo."""
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    if not settings.openai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    try:
        vectors = await embed_texts([query], settings=settings)
        hits = await search_similar(vectors[0], limit=body.limit, settings=settings)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similarity search failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Similarity search unavailable: {exc}",
        ) from exc

    object_ids: list[ObjectId] = []
    scores_by_id: dict[str, float] = {}
    for mongo_id, score in hits:
        try:
            object_id = ObjectId(mongo_id)
        except Exception:
            continue
        key = str(object_id)
        if key in scores_by_id:
            continue
        object_ids.append(object_id)
        scores_by_id[key] = score

    docs_by_id: dict[str, dict[str, Any]] = {}
    if object_ids:
        cursor = db.leads.find({"_id": {"$in": object_ids}})
        async for doc in cursor:
            docs_by_id[str(doc["_id"])] = doc

    results: list[SimilarityHitOut] = []
    for object_id in object_ids:
        key = str(object_id)
        doc = docs_by_id.get(key)
        if not doc:
            continue
        results.append(
            SimilarityHitOut(score=scores_by_id.get(key, 0.0), lead=_serialize_lead(doc))
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
