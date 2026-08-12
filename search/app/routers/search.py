import asyncio
import csv
import io
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import quote

import orjson
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status, Query
from fastapi.responses import StreamingResponse
from fm_runtime import JobStatus, current_principal
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, oauth2_scheme
from app.config import Settings, get_settings
from app.database import SessionLocal, get_db
from app.jobs_registry import (
    JOB_TYPE_APOLLO_SEARCH,
    JOB_TYPE_EMBEDDING,
    JobContext,
    job_progress_fraction,
    publish_job,
)
from app.leads_client import (
    APOLLO_MAX_PER_PAGE,
    LeadsClient,
    lead_to_record,
    normalize_domain,
)
from app.models import SearchHistory, SearchResult
from app.schemas import (
    ImportSearchRequest,
    CompanyPeopleSearchRequest,
    HistoryOwnerOut,
    SearchHistoryDetail,
    SearchHistorySummary,
    SearchRequest,
    SearchResponse,
    SimilarityHitOut,
    SimilaritySearchRequest,
    SimilaritySearchResponse,
    UserOut,
)

logger = logging.getLogger(__name__)


def _principal_attribution() -> tuple[str, str]:
    """(origin, actor) of the acting principal for record attribution.

    origin = ``user``/``agent`` (fm_origin); actor = the exchanging client (azp).
    Defaults to a direct human call when no principal is present (the route is
    non-anonymous, so the middleware has already ensured one on real requests)."""
    principal = current_principal()
    if principal is None:
        return "user", ""
    return principal.origin, principal.actor

router = APIRouter(prefix="/api/search", tags=["search"])

# Per-page size for leads stream jobs (leads walks Apollo pages server-side).
_APOLLO_FETCH_PER_PAGE = APOLLO_MAX_PER_PAGE
_UI_PER_PAGE = APOLLO_MAX_PER_PAGE
_LEADS_BY_MONGO_IDS_MAX = 500
_CSV_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# SearchHistory.query column size.
_HISTORY_QUERY_MAX = 512

# Search jobs run detached from the browser connection so a disconnect doesn't
# stop id persistence; strong refs keep them alive until done.
_search_jobs: set[asyncio.Task[None]] = set()


class SearchPageRequest(BaseModel):
    page: int = Field(ge=1)


def _clip_history_query(label: str) -> str:
    if len(label) <= _HISTORY_QUERY_MAX:
        return label
    return f"{label[: _HISTORY_QUERY_MAX - 3]}..."


def _history_label_for_company_people(company_name: str, keywords: str | None) -> str:
    base = f"people @ {company_name.strip()}"
    cleaned = " ".join((keywords or "").split()).strip()
    return f"{base}: {cleaned}" if cleaned else base


def _history_label_for_company_search(
    keywords: str | None,
    company_name: str | None,
    company_domain: str | None = None,
) -> str:
    tags = " ".join((keywords or "").split()).strip()
    name = " ".join((company_name or "").split()).strip()
    domain = " ".join((company_domain or "").split()).strip()
    parts = [part for part in (name, domain, tags) if part]
    return " · ".join(parts) if parts else "company search"


def _history_label_for_people_search(
    query: str,
    *,
    organization_id: str | None,
    organization_name: str | None,
    resolved_organization: dict | None,
    organization_display_name: str | None = None,
) -> str:
    base = query.strip()
    company_label: str | None = None
    if resolved_organization and resolved_organization.get("name"):
        company_label = str(resolved_organization["name"])
    elif organization_name and organization_name.strip():
        company_label = organization_name.strip()
    elif organization_display_name and organization_display_name.strip():
        company_label = organization_display_name.strip()
    elif organization_id and organization_id.strip():
        company_label = f"org:{organization_id.strip()}"

    if base and company_label:
        return f"{base} @ {company_label}"
    if company_label:
        return f"people @ {company_label}"
    return base or "people search"


def _build_search_params(
    *,
    entity_type: str,
    query: str = "",
    organization_id: str | None = None,
    organization_name: str | None = None,
    organization_display_name: str | None = None,
    domain: str | None = None,
    company_name: str | None = None,
    company_domain: str | None = None,
    keywords: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "query": query,
        "organization_id": organization_id,
        "organization_name": organization_name,
        "organization_display_name": organization_display_name,
        "domain": domain,
        "company_name": company_name,
        "company_domain": company_domain,
        "keywords": keywords,
    }


async def _count_stored_results(db: AsyncSession, search_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(SearchResult).where(SearchResult.search_id == search_id)
    )
    return int(result.scalar_one())


async def _load_page_mongo_ids(
    db: AsyncSession,
    *,
    search_id: int,
    page: int,
    per_page: int,
) -> list[str]:
    offset = max(page - 1, 0) * per_page
    result = await db.execute(
        select(SearchResult.external_id)
        .where(SearchResult.search_id == search_id)
        .order_by(SearchResult.position.asc())
        .offset(offset)
        .limit(per_page)
    )
    return [str(row[0]) for row in result.all()]


async def _load_all_mongo_ids(db: AsyncSession, *, search_id: int) -> list[str]:
    result = await db.execute(
        select(SearchResult.external_id)
        .where(SearchResult.search_id == search_id)
        .order_by(SearchResult.position.asc())
    )
    return [str(row[0]) for row in result.all()]


def _email_from_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                email = item.get("email")
                if isinstance(email, str) and email.strip():
                    return email.strip()
    return None


def _resolved_record_email(record: dict[str, Any]) -> str | None:
    """Mirror frontend resolvedPersonEmail: top-level, then people/match payload."""
    direct = _email_from_value(record.get("email")) or _email_from_value(record.get("emails"))
    if direct:
        return direct
    responses = record.get("apollo_responses")
    if not isinstance(responses, dict):
        return None
    entry = responses.get("/api/v1/people/match")
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    person = data.get("person")
    if isinstance(person, dict):
        nested = _email_from_value(person.get("email")) or _email_from_value(person.get("emails"))
        if nested:
            return nested
    return _email_from_value(data.get("email")) or _email_from_value(data.get("emails"))


_CSV_HEADER = ["mongo_id", "name", "email", "linkedin", "phone", "company", "title"]


def _clean_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _phones_from_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            number = item.get("sanitized_number") or item.get("raw_number") or item.get("number")
            if isinstance(number, str) and number.strip():
                out.append(number.strip())
    return out


def _resolved_record_phone(record: dict[str, Any]) -> str | None:
    """Mirror frontend resolvedRecordPhone: person phone_numbers (top-level, then
    people/match payload) joined; company top-level phone string."""
    if record.get("entity_type") == "person":
        direct = _phones_from_value(record.get("phone_numbers"))
        if direct:
            return "; ".join(direct)
        responses = record.get("apollo_responses")
        if isinstance(responses, dict):
            entry = responses.get("/api/v1/people/match")
            data = entry.get("data") if isinstance(entry, dict) else None
            if isinstance(data, dict):
                person = data.get("person")
                if isinstance(person, dict):
                    nested = _phones_from_value(person.get("phone_numbers"))
                    if nested:
                        return "; ".join(nested)
                top = _phones_from_value(data.get("phone_numbers"))
                if top:
                    return "; ".join(top)
        return None
    return _clean_str(record.get("phone"))


def _record_company(record: dict[str, Any]) -> str | None:
    if record.get("entity_type") == "person":
        org = record.get("organization")
        return _clean_str(org.get("name")) if isinstance(org, dict) else None
    return _clean_str(record.get("name"))


def _record_title(record: dict[str, Any]) -> str | None:
    if record.get("entity_type") != "person":
        return None
    return _clean_str(record.get("title") or record.get("headline"))


def _csv_export_filename(search: SearchHistory) -> str:
    slug = _CSV_FILENAME_SAFE.sub("-", (search.query or "search").strip())[:48].strip("-._")
    if not slug:
        slug = "search"
    return f"search-{search.id}-{slug}.csv"


def _csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection: Excel/Sheets evaluate cells
    starting with = + - @ (or tab/CR) even when the CSV quotes them."""
    if value and value[0] in "=+-@\t\r":
        return f"'{value}"
    return value


async def _iter_search_csv(client: LeadsClient, mongo_ids: list[str]) -> AsyncIterator[str]:
    """Hydrate and emit rows chunk by chunk so bytes flow immediately.

    Buffering all rows first stalls large exports past nginx's proxy timeout.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def _flush() -> str:
        value = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return value

    writer.writerow(_CSV_HEADER)
    yield _flush()
    # Hydration now goes through the token-authorized leads backend, so a chunk
    # can fail mid-export (expired token, OPA deny, leads down). Never raise out
    # of a started download — stop cleanly and append a visible sentinel row so
    # the truncation is detectable rather than a silently short file.
    try:
        for start in range(0, len(mongo_ids), _LEADS_BY_MONGO_IDS_MAX):
            records = await _hydrate_results(
                client, mongo_ids[start : start + _LEADS_BY_MONGO_IDS_MAX]
            )
            for record in records:
                name = str(record.get("name") or "").strip() or "null"
                email = _resolved_record_email(record) or "null"
                phone = _resolved_record_phone(record) or "null"
                company = _record_company(record) or "null"
                title = _record_title(record) or "null"
                mongo_id = str(record.get("mongo_id") or "").strip() or "null"
                linkedin = str(record.get("linkedin_url") or "").strip() or "null"
                writer.writerow(
                    [
                        _csv_cell(mongo_id),
                        _csv_cell(name),
                        _csv_cell(email),
                        _csv_cell(linkedin),
                        _csv_cell(phone),
                        _csv_cell(company),
                        _csv_cell(title),
                    ]
                )
            yield _flush()
    except Exception as exc:
        logger.warning("CSV export truncated after hydrate failure: %s", exc)
        writer.writerow(
            ["ERROR: export truncated before all rows were written"]
            + [""] * (len(_CSV_HEADER) - 1)
        )
        yield _flush()


async def _hydrate_results(
    client: LeadsClient,
    mongo_ids: list[str],
) -> list[dict[str, Any]]:
    if not mongo_ids:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for start in range(0, len(mongo_ids), _LEADS_BY_MONGO_IDS_MAX):
        chunk = mongo_ids[start : start + _LEADS_BY_MONGO_IDS_MAX]
        leads = await client.get_by_mongo_ids(chunk)
        for lead in leads:
            by_id[str(lead.get("id"))] = lead
    results: list[dict[str, Any]] = []
    for mongo_id in mongo_ids:
        lead = by_id.get(mongo_id)
        if not lead:
            results.append(
                {
                    "id": mongo_id,
                    "mongo_id": mongo_id,
                    "entity_type": "person",
                    "name": f"(missing lead {mongo_id})",
                }
            )
            continue
        record = lead_to_record(lead)
        if record:
            results.append(record)
        else:
            results.append(
                {
                    "id": lead.get("apollo_id") or mongo_id,
                    "mongo_id": mongo_id,
                    "entity_type": "person" if lead.get("entity_type") == "person" else "company",
                    "name": str(lead.get("apollo_id") or mongo_id),
                    "raw": {},
                    "apollo_responses": lead.get("apollo_responses") or {},
                }
            )
    return results


async def _append_unique_mongo_ids(
    db: AsyncSession,
    *,
    search: SearchHistory,
    mongo_ids: list[str],
    seen: set[str],
    next_position: int,
) -> int:
    """Insert unique Mongo `_id`s. Returns how many new rows were added."""
    added = 0
    entity = "person" if search.entity_type in {"people", "person"} else "company"
    for mongo_id in mongo_ids:
        external_id = str(mongo_id or "").strip()
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        db.add(
            SearchResult(
                search_id=search.id,
                external_id=external_id,
                entity_type=entity,
                position=next_position + added,
            )
        )
        added += 1
    return added


def _ndjson_line(payload: dict[str, Any]) -> str:
    # orjson serializes datetime natively; ``default=str`` preserves the prior
    # fallback for any other non-JSON-native value that slips into an event.
    return orjson.dumps(payload, default=str).decode() + "\n"


async def _apollo_params_for_stream(
    client: LeadsClient,
    *,
    params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build Apollo search params and optional meta (e.g. resolved org)."""
    entity_type = str(params.get("entity_type") or "")
    meta: dict[str, Any] = {}

    if entity_type == "people":
        org_id = (params.get("organization_id") or "").strip() or None
        org_name = " ".join(str(params.get("organization_name") or "").split()).strip() or None
        domain = normalize_domain(
            params.get("organization_domain") or params.get("domain")
        )
        if org_id and org_name:
            raise HTTPException(
                status_code=400,
                detail="Provide either organization_id or organization_name, not both",
            )
        if org_name:
            resolved = await client.resolve_organization_by_name(org_name)
            org_id = resolved["id"]
            domain = domain or normalize_domain(resolved.get("domain"))
            meta["resolved_organization"] = resolved

        cleaned_query = str(params.get("query") or "").strip() or None
        if not cleaned_query and not org_id and not domain:
            raise HTTPException(
                status_code=400,
                detail="people search requires search text and/or a company filter",
            )
        apollo_params = client.build_people_params(
            query=cleaned_query,
            page=1,
            per_page=_APOLLO_FETCH_PER_PAGE,
            organization_ids=[org_id] if org_id else None,
            organization_domains=[domain] if domain and not org_id else None,
        )
        return apollo_params, meta

    if entity_type == "companies":
        keywords = str(params.get("keywords") or params.get("query") or "")
        apollo_params = client.build_company_params(
            keywords=keywords,
            page=1,
            per_page=_APOLLO_FETCH_PER_PAGE,
            company_name=params.get("company_name"),
            company_domain=params.get("company_domain") or params.get("domain"),
        )
        return apollo_params, meta

    raise HTTPException(status_code=400, detail="Unsupported search entity type")


async def _ingest_leads_via_stream(
    db: AsyncSession,
    client: LeadsClient,
    *,
    search: SearchHistory,
    params: dict[str, Any],
    job_ctx: JobContext | None = None,
    on_started: Callable[[str, str | None], Awaitable[None]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Start leads ingest + embedding streams; persist Mongo `_id`s as pages arrive.

    When ``job_ctx`` is set this doubles as a **job producer**: it publishes
    ``JobEvent``s (job_id = the leads stream id, so control maps 1:1) to the
    in-process registry that ``/internal/jobs/v1/stream`` fans out to the jobs
    service. ``on_started`` (if given) is awaited once the leads streams exist,
    so a non-streaming caller (the MCP surface) can return the stream handles
    while the ingest keeps running detached.
    """
    entity_type = str(params.get("entity_type") or search.entity_type)
    apollo_params, meta = await _apollo_params_for_stream(client, params=params)

    resolved = meta.get("resolved_organization")
    if entity_type == "people" and resolved and resolved.get("name"):
        params = {
            **params,
            "organization_id": resolved.get("id") or params.get("organization_id"),
            "organization_display_name": resolved.get("name"),
            "organization_name": None,
        }
        search.search_params_json = json.dumps(params)
        await db.commit()

    if entity_type == "people":
        stream_handles = await client.start_people_search_stream(apollo_params)
    elif entity_type == "companies":
        stream_handles = await client.start_organizations_search_stream(apollo_params)
    else:
        raise HTTPException(status_code=400, detail="Unsupported search entity type")

    ingest_stream_id = str(stream_handles.get("ingest_stream_id") or "").strip()
    embedding_stream_id = str(stream_handles.get("embedding_stream_id") or "").strip() or None
    if not ingest_stream_id:
        raise HTTPException(status_code=502, detail="Leads search missing ingest_stream_id")

    # The leads streams now exist: hand their ids back (MCP non-streaming start)
    # and register the jobs so they surface in the jobs service immediately.
    if on_started is not None:
        await on_started(ingest_stream_id, embedding_stream_id)
    await publish_job(
        job_id=ingest_stream_id,
        job_type=JOB_TYPE_APOLLO_SEARCH,
        ctx=job_ctx,
        status=JobStatus.RUNNING,
        progress=0.0,
        meta={"kind": "ingest", "embedding_stream_id": embedding_stream_id},
    )
    if embedding_stream_id and job_ctx is not None:
        await publish_job(
            job_id=embedding_stream_id,
            job_type=JOB_TYPE_EMBEDDING,
            ctx=job_ctx,
            status=JobStatus.QUEUED,
            progress=0.0,
            meta={"kind": "embedding", "ingest_stream_id": ingest_stream_id},
        )

    subscribe_ids = [ingest_stream_id]
    if embedding_stream_id:
        subscribe_ids.append(embedding_stream_id)

    seen: set[str] = set()
    position = 0
    ingest_done = False
    embedding_done = embedding_stream_id is None

    yield {
        "type": "progress",
        "kind": "ingest",
        "page": 0,
        "total_pages": 1,
        "stored": 0,
        "ingest_stream_id": ingest_stream_id,
        "embedding_stream_id": embedding_stream_id,
    }

    async for event in client.subscribe_streams(subscribe_ids):
        event_stream_id = str(event.get("stream_id") or "").strip()
        event_kind = str(event.get("kind") or "").strip()
        if not event_kind:
            event_kind = (
                "embedding" if embedding_stream_id and event_stream_id == embedding_stream_id else "ingest"
            )
        event_type = event.get("type")

        if event_kind == "embedding":
            if event_type == "progress":
                done = int(event.get("done") or 0)
                total = int(event.get("total") or 0)
                await publish_job(
                    job_id=embedding_stream_id or "",
                    job_type=JOB_TYPE_EMBEDDING,
                    ctx=job_ctx,
                    status=JobStatus.RUNNING,
                    progress=job_progress_fraction(done, total),
                    meta={"kind": "embedding", "done": done, "total": total},
                )
                yield {
                    "type": "embedding_progress",
                    "kind": "embedding",
                    "done": done,
                    "total": total,
                    "embedding_stream_id": embedding_stream_id,
                }
            elif event_type == "complete":
                embedding_done = True
                total = int(event.get("total") or 0)
                await publish_job(
                    job_id=embedding_stream_id or "",
                    job_type=JOB_TYPE_EMBEDDING,
                    ctx=job_ctx,
                    status=JobStatus.COMPLETED,
                    progress=1.0,
                    exit_status="ok",
                    meta={"kind": "embedding", "total": total},
                )
                yield {
                    "type": "embedding_progress",
                    "kind": "embedding",
                    "done": int(event.get("done") or event.get("total") or 0),
                    "total": total,
                    "complete": True,
                    "embedding_stream_id": embedding_stream_id,
                }
            elif event_type == "error":
                embedding_done = True
                detail = str(event.get("detail") or "Embedding failed")
                await publish_job(
                    job_id=embedding_stream_id or "",
                    job_type=JOB_TYPE_EMBEDDING,
                    ctx=job_ctx,
                    status=JobStatus.FAILED,
                    exit_status=detail,
                    meta={"kind": "embedding"},
                )
                yield {
                    "type": "embedding_progress",
                    "kind": "embedding",
                    "done": int(event.get("done") or 0),
                    "total": int(event.get("total") or 0),
                    "error": detail,
                    "embedding_stream_id": embedding_stream_id,
                }
        else:
            if event_type == "ids":
                raw_ids = event.get("ids") or []
                mongo_ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
                added = await _append_unique_mongo_ids(
                    db,
                    search=search,
                    mongo_ids=mongo_ids,
                    seen=seen,
                    next_position=position,
                )
                position += added
                search.total_results = position
                await db.commit()
                page = int(event.get("page") or 0)
                total_pages = int(event.get("total_pages") or 1)
                await publish_job(
                    job_id=ingest_stream_id,
                    job_type=JOB_TYPE_APOLLO_SEARCH,
                    ctx=job_ctx,
                    status=JobStatus.RUNNING,
                    progress=job_progress_fraction(page, total_pages),
                    meta={
                        "kind": "ingest",
                        "stored": position,
                        "page": page,
                        "total_pages": total_pages,
                    },
                )
                yield {
                    "type": "progress",
                    "kind": "ingest",
                    "page": page,
                    "total_pages": total_pages,
                    "stored": position,
                    "ids": mongo_ids,
                    "ingest_stream_id": ingest_stream_id,
                    "embedding_stream_id": embedding_stream_id,
                }
            elif event_type == "error":
                detail = event.get("detail") or "Leads stream failed"
                await publish_job(
                    job_id=ingest_stream_id,
                    job_type=JOB_TYPE_APOLLO_SEARCH,
                    ctx=job_ctx,
                    status=JobStatus.FAILED,
                    exit_status=str(detail),
                    meta={"kind": "ingest", "stored": position},
                )
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
            elif event_type == "complete":
                ingest_done = True
                search.total_results = position
                await db.commit()
                await db.refresh(search)
                await publish_job(
                    job_id=ingest_stream_id,
                    job_type=JOB_TYPE_APOLLO_SEARCH,
                    ctx=job_ctx,
                    status=JobStatus.COMPLETED,
                    progress=1.0,
                    exit_status="ok",
                    meta={"kind": "ingest", "stored": position},
                )
                yield {
                    "type": "ingest_complete",
                    "kind": "ingest",
                    "stored": position,
                    "ingest_stream_id": ingest_stream_id,
                    "embedding_stream_id": embedding_stream_id,
                }

        if ingest_done and embedding_done:
            return

    if not ingest_done:
        search.total_results = position
        await db.commit()
        await db.refresh(search)
        await publish_job(
            job_id=ingest_stream_id,
            job_type=JOB_TYPE_APOLLO_SEARCH,
            ctx=job_ctx,
            status=JobStatus.COMPLETED,
            progress=1.0,
            exit_status="ok",
            meta={"kind": "ingest", "stored": position},
        )
        yield {
            "type": "ingest_complete",
            "kind": "ingest",
            "stored": position,
            "ingest_stream_id": ingest_stream_id,
            "embedding_stream_id": embedding_stream_id,
        }


async def _search_ndjson_events(
    db: AsyncSession,
    client: LeadsClient,
    *,
    search: SearchHistory,
    params: dict[str, Any],
    job_ctx: JobContext | None = None,
    on_started: Callable[[str, str | None], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    """Emit ingest/embedding progress, first_page ASAP, then search complete after ingest.

    Embedding progress may continue after ``complete``. Page navigation stays on the
    synchronous ``POST /searches/{id}/page`` endpoint. ``job_ctx``/``on_started`` are
    threaded to ``_ingest_leads_via_stream`` for job publication + non-streaming start.
    """
    first_page_sent = False
    search_complete_sent = False
    per_page = max(search.per_page or _UI_PER_PAGE, 1)
    page_results: list[dict[str, Any]] = []

    async def _emit_first_page_if_needed() -> AsyncIterator[str]:
        nonlocal first_page_sent, page_results
        if first_page_sent:
            return
        page_results = await _set_current_page(db, client, search, 1)
        response = SearchResponse(
            history=_to_detail(search, page_results),
            pagination=_pagination_for(search),
        )
        yield _ndjson_line({"type": "first_page", **response.model_dump(mode="json")})
        first_page_sent = True

    async def _emit_search_complete() -> AsyncIterator[str]:
        nonlocal search_complete_sent, page_results
        if search_complete_sent:
            return
        if not first_page_sent:
            async for line in _emit_first_page_if_needed():
                yield line
        else:
            # Re-hydrate whatever page the row points to now: the user may have
            # paginated (sync /page endpoint) while ingest was still running, and
            # emitting the cached page-1 results with a refreshed page number
            # would mislabel them and yank the user off the page they're viewing.
            await db.refresh(search)
            page_results = await _set_current_page(db, client, search, search.page or 1)
        response = SearchResponse(
            history=_to_detail(search, page_results),
            pagination=_pagination_for(search),
        )
        yield _ndjson_line({"type": "complete", **response.model_dump(mode="json")})
        search_complete_sent = True

    async for event in _ingest_leads_via_stream(
        db,
        client,
        search=search,
        params=params,
        job_ctx=job_ctx,
        on_started=on_started,
    ):
        event_type = event.get("type")
        if event_type == "embedding_progress":
            yield _ndjson_line(event)
            continue

        if event_type == "ingest_complete":
            async for line in _emit_search_complete():
                yield line
            continue

        yield _ndjson_line(event)
        if (
            not first_page_sent
            and event_type == "progress"
            and event.get("kind", "ingest") == "ingest"
            and int(event.get("stored") or 0) >= per_page
        ):
            async for line in _emit_first_page_if_needed():
                yield line

    if not search_complete_sent:
        async for line in _emit_search_complete():
            yield line


async def _set_current_page(
    db: AsyncSession,
    client: LeadsClient,
    search: SearchHistory,
    page: int,
) -> list[dict[str, Any]]:
    per_page = search.per_page or _UI_PER_PAGE
    mongo_ids = await _load_page_mongo_ids(
        db,
        search_id=search.id,
        page=page,
        per_page=per_page,
    )
    page_results = await _hydrate_results(client, mongo_ids)
    search.page = page
    # Cache hydrated page for quick reopen; source of truth remains leads + mongo ids.
    # TEXT column stores str, so decode orjson's bytes.
    search.results_json = orjson.dumps(page_results).decode()
    await db.commit()
    await db.refresh(search)
    return page_results


def _to_detail(row: SearchHistory, results: list[dict[str, Any]] | None = None) -> SearchHistoryDetail:
    if results is None:
        try:
            # orjson.JSONDecodeError subclasses json.JSONDecodeError, so the
            # existing except still catches a malformed cache blob.
            results = orjson.loads(row.results_json or "[]")
        except json.JSONDecodeError:
            results = []
        if not isinstance(results, list):
            results = []
    return SearchHistoryDetail(
        id=row.id,
        query=row.query,
        entity_type=row.entity_type,
        page=row.page,
        per_page=row.per_page,
        total_results=row.total_results,
        created_at=row.created_at,
        # Attribution so any viewer (cross-user) sees whose search it is +
        # "alice (via agent)". Legacy rows default to a direct human call.
        username=row.username,
        origin=row.origin,
        actor=row.actor,
        results=results,
    )


def _pagination_for(row: SearchHistory) -> dict[str, Any]:
    per_page = max(row.per_page or _UI_PER_PAGE, 1)
    total = max(row.total_results or 0, 0)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return {
        "page": row.page,
        "per_page": per_page,
        "total_entries": total,
        "total_pages": total_pages,
    }


async def _get_owned_search(db: AsyncSession, search_id: int, user: UserOut) -> SearchHistory:
    """Load a search owned by the caller; another user's row 404s like a missing one.

    Retained only for the **mutating** delete path — a user may remove their own
    history but not another's. READ paths use :func:`_get_search_any` (principle 1:
    any search-access principal may browse any user's history; row ownership is
    not an access gate)."""
    row = await db.get(SearchHistory, search_id)
    if not row or row.username != user.username:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    return row


async def _get_search_any(db: AsyncSession, search_id: int) -> SearchHistory:
    """Load any search by id, regardless of owner (cross-user read).

    Access is already gated by the search-access role (mesh OPA / in-process
    grants); this deliberately does NOT filter by ``username``."""
    row = await db.get(SearchHistory, search_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    return row


async def _run_search_job(
    settings: Settings,
    token: str | None,
    *,
    username: str,
    origin: str,
    actor: str,
    history_query: str,
    entity_type: str,
    per_page: int,
    search_params: dict[str, Any],
    emit: Callable[[str | None], Awaitable[None]],
    started_future: asyncio.Future[dict[str, Any]] | None = None,
) -> None:
    """Detached search job: create the history row, run the leads ingest, persist
    ids, publish job events. Shared by the UI streaming path and the MCP
    non-streaming start.

    ``emit`` receives each NDJSON line (a queue push for streaming, or a sink that
    drops them for MCP) and a terminal ``None``. ``started_future`` (MCP) resolves
    with ``{search_id, ingest_stream_id, embedding_stream_id}`` once the leads
    streams exist, so the caller can return handles while ingest keeps running.

    Never raises out of the streaming relay: every failure becomes an ``error``
    line via ``emit`` (and a FAILED job event upstream). Only ``started_future``
    carries an exception, and only before the response has started.
    """
    client = LeadsClient(settings, token)
    try:
        async with SessionLocal() as job_db:
            row = SearchHistory(
                username=username,
                origin=origin,
                actor=actor,
                query=_clip_history_query(history_query),
                entity_type=entity_type,
                page=1,
                per_page=per_page,
                total_results=0,
                results_json="[]",
                search_params_json=json.dumps(search_params),
            )
            job_db.add(row)
            await job_db.commit()
            await job_db.refresh(row)
            job_ctx = JobContext(
                user=username,
                origin=origin,
                actor=actor,
                search_id=row.id,
                job_type=JOB_TYPE_APOLLO_SEARCH,
            )

            async def _on_started(ingest_id: str, embedding_id: str | None) -> None:
                if started_future is not None and not started_future.done():
                    started_future.set_result(
                        {
                            "search_id": row.id,
                            "ingest_stream_id": ingest_id,
                            "embedding_stream_id": embedding_id,
                        }
                    )

            async for line in _search_ndjson_events(
                job_db,
                client,
                search=row,
                params=search_params,
                job_ctx=job_ctx,
                on_started=_on_started,
            ):
                await emit(line)
    except HTTPException as exc:
        if started_future is not None and not started_future.done():
            started_future.set_exception(exc)
        await emit(_ndjson_line({"type": "error", "detail": exc.detail}))
    except Exception as exc:
        logger.exception("Search stream job failed")
        if started_future is not None and not started_future.done():
            started_future.set_exception(exc)
        await emit(_ndjson_line({"type": "error", "detail": f"Search failed: {exc}"}))
    finally:
        await emit(None)


def _spawn_search_job(coro: Awaitable[None], *, name: str) -> asyncio.Task[None]:
    """Start a detached search job and keep a strong ref until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _search_jobs.add(task)
    task.add_done_callback(_search_jobs.discard)
    return task


def _search_stream_response(
    settings: Settings,
    token: str,
    *,
    username: str,
    origin: str,
    actor: str,
    history_query: str,
    entity_type: str,
    per_page: int,
    search_params: dict[str, Any],
) -> StreamingResponse:
    """Run the search as a detached job and relay its NDJSON events to the client.

    The job owns its DB session and keeps persisting Mongo ids even if the
    browser disconnects mid-stream (explicit cancellation goes through
    ``POST /streams/{id}/cancel``). Once the NDJSON response has started the
    stream must never raise — Starlette would reset the connection, the browser
    reports a generic "network error", and every other stream sharing that
    origin appears to die — so every failure (HTTP, transport, DB) is emitted
    as a graceful per-stream ``error`` event instead.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    relay_alive = True

    async def _emit(line: str | None) -> None:
        if relay_alive or line is None:
            await queue.put(line)

    _spawn_search_job(
        _run_search_job(
            settings,
            token,
            username=username,
            origin=origin,
            actor=actor,
            history_query=history_query,
            entity_type=entity_type,
            per_page=per_page,
            search_params=search_params,
            emit=_emit,
        ),
        name="search-stream-job",
    )

    async def event_stream() -> AsyncIterator[str]:
        nonlocal relay_alive
        try:
            while True:
                line = await queue.get()
                if line is None:
                    break
                yield line
        finally:
            # Browser gone (or stream done): stop queueing lines but let the
            # job keep persisting ids until the leads ingest finishes.
            relay_alive = False

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _start_search_job_detached(
    settings: Settings,
    token: str,
    *,
    username: str,
    origin: str,
    actor: str,
    history_query: str,
    entity_type: str,
    per_page: int,
    search_params: dict[str, Any],
) -> dict[str, Any]:
    """Start a detached search and return once the leads streams exist (MCP surface).

    The job keeps running (persisting ids + publishing job events) after this
    returns; the caller watches progress via the jobs service. Errors before the
    streams start propagate as an exception (a synchronous handler, not a stream)."""
    loop = asyncio.get_running_loop()
    started_future: asyncio.Future[dict[str, Any]] = loop.create_future()

    async def _sink(_line: str | None) -> None:
        # MCP start is non-streaming: the detached job's NDJSON lines are dropped;
        # progress is observed via the jobs service, results via the read APIs.
        return

    _spawn_search_job(
        _run_search_job(
            settings,
            token,
            username=username,
            origin=origin,
            actor=actor,
            history_query=history_query,
            entity_type=entity_type,
            per_page=per_page,
            search_params=search_params,
            emit=_sink,
            started_future=started_future,
        ),
        name="search-mcp-job",
    )
    return await started_future


@router.post("/search")
async def search(
    body: SearchRequest,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> StreamingResponse:
    """NDJSON ingest stream: emit page 1 as soon as filled; further pages via sync endpoint."""
    client = LeadsClient(settings, token)
    company_name = (body.company_name or "").strip() or None
    company_domain = (body.company_domain or "").strip() or None

    # Resolve org name once for history labeling (also warms leads cache).
    resolved: dict[str, Any] | None = None
    if body.entity_type == "people" and (body.organization_name or "").strip():
        resolved = await client.resolve_organization_by_name(body.organization_name.strip())

    if body.entity_type == "people":
        history_query = _history_label_for_people_search(
            body.query,
            organization_id=body.organization_id,
            organization_name=body.organization_name,
            resolved_organization=resolved,
            organization_display_name=body.organization_display_name,
        )
    else:
        history_query = _history_label_for_company_search(
            body.query, company_name, company_domain
        )

    org_id = body.organization_id
    display_name = body.organization_display_name
    if resolved:
        org_id = resolved.get("id") or org_id
        display_name = resolved.get("name") or display_name

    search_params = _build_search_params(
        entity_type=body.entity_type,
        query=body.query.strip(),
        organization_id=org_id,
        organization_name=None if org_id else body.organization_name,
        organization_display_name=display_name,
        domain=body.organization_domain if body.entity_type == "people" else None,
        company_name=company_name if body.entity_type == "companies" else None,
        company_domain=company_domain if body.entity_type == "companies" else None,
        keywords=body.query.strip() if body.entity_type == "companies" else None,
    )

    origin, actor = _principal_attribution()
    return _search_stream_response(
        settings,
        token,
        username=user.username,
        origin=origin,
        actor=actor,
        history_query=history_query,
        entity_type=body.entity_type,
        per_page=body.per_page or _UI_PER_PAGE,
        search_params=search_params,
    )


@router.post("/search/company-people")
async def search_company_people(
    body: CompanyPeopleSearchRequest,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> StreamingResponse:
    history_query = _history_label_for_company_people(body.company_name, body.keywords)
    search_params = _build_search_params(
        entity_type="people",
        query=(body.keywords or "").strip(),
        organization_id=body.organization_id,
        organization_display_name=body.company_name,
        domain=body.domain,
        company_name=body.company_name,
        keywords=body.keywords,
    )

    origin, actor = _principal_attribution()
    return _search_stream_response(
        settings,
        token,
        username=user.username,
        origin=origin,
        actor=actor,
        history_query=history_query,
        entity_type="people",
        per_page=body.per_page or _UI_PER_PAGE,
        search_params=search_params,
    )


@router.post("/searches/{search_id}/page", response_model=SearchResponse)
async def search_page(
    search_id: int,
    body: SearchPageRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> SearchResponse:
    # Cross-user read (principle 1): any search-access principal may page any
    # user's search. ``results_json``/``page`` on the row are a shared page cache,
    # not a per-user view.
    source = await _get_search_any(db, search_id)

    stored = await _count_stored_results(db, search_id)
    if stored == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This search has no stored results to paginate",
        )

    if source.total_results != stored:
        source.total_results = stored

    client = LeadsClient(settings, token)
    results = await _set_current_page(db, client, source, body.page)
    return SearchResponse(history=_to_detail(source, results), pagination=_pagination_for(source))


@router.get("/searches", response_model=list[SearchHistorySummary])
async def list_searches(
    username: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: UserOut = Depends(get_current_user),
) -> list[SearchHistory]:
    """List searches. Cross-user by default (principle 1); ``?username=`` narrows
    to one owner's history for the browse-by-user view."""
    stmt = select(SearchHistory).order_by(SearchHistory.created_at.desc()).limit(200)
    if username is not None:
        stmt = (
            select(SearchHistory)
            .where(SearchHistory.username == username)
            .order_by(SearchHistory.created_at.desc())
            .limit(200)
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/users", response_model=list[HistoryOwnerOut])
async def list_history_owners(
    db: AsyncSession = Depends(get_db),
    _: UserOut = Depends(get_current_user),
) -> list[HistoryOwnerOut]:
    """Distinct history owners + their search counts — the browse-by-user index
    (principle 1: anyone may see whose searches exist). Empty-username legacy rows
    are skipped."""
    result = await db.execute(
        select(SearchHistory.username, func.count())
        .where(SearchHistory.username != "")
        .group_by(SearchHistory.username)
        .order_by(func.count().desc())
    )
    return [
        HistoryOwnerOut(username=str(username), search_count=int(count))
        for username, count in result.all()
    ]


@router.get("/searches/{search_id}", response_model=SearchHistoryDetail)
async def get_search(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> SearchHistoryDetail:
    # Cross-user read (principle 1).
    row = await _get_search_any(db, search_id)

    stored = await _count_stored_results(db, search_id)
    if stored > 0:
        if row.total_results != stored:
            row.total_results = stored
            await db.commit()
            await db.refresh(row)
        client = LeadsClient(settings, token)
        mongo_ids = await _load_page_mongo_ids(
            db,
            search_id=row.id,
            page=row.page or 1,
            per_page=row.per_page or _UI_PER_PAGE,
        )
        results = await _hydrate_results(client, mongo_ids)
        return _to_detail(row, results)

    return _to_detail(row)


@router.delete("/searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_search(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserOut = Depends(get_current_user),
) -> None:
    row = await _get_owned_search(db, search_id, user)
    await db.execute(delete(SearchResult).where(SearchResult.search_id == search_id))
    await db.delete(row)
    await db.commit()


@router.post("/streams/{stream_id}/cancel")
async def cancel_stream_job(
    stream_id: str,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> dict[str, object]:
    """Cancel a running leads ingest or embedding stream."""
    cleaned = stream_id.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="stream_id is required")
    client = LeadsClient(settings, token=token)
    return await client.cancel_stream(cleaned)


@router.post("/searches/import", response_model=SimilaritySearchResponse)
async def import_search_from_ids(
    body: ImportSearchRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> SimilaritySearchResponse:
    """Create a search from a caller-supplied list of lead Mongo `_id`s.

    Backs CSV re-import (export -> external triage -> re-import to enrich).
    Ids are validated against leads (unknown ones dropped and counted); order
    is preserved; page 1 is cached like any other search.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for value in body.ids:
        raw = (value or "").strip()
        if raw and raw not in seen:
            seen.add(raw)
            ordered.append(raw)
    client = LeadsClient(settings, token)
    present = await client.exists_by_mongo_ids(ordered)
    valid = [mongo_id for mongo_id in ordered if mongo_id in present]
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="None of the supplied ids match stored leads",
        )
    page_records = await _hydrate_results(client, valid[:_UI_PER_PAGE])
    origin, actor = _principal_attribution()
    label = (body.label or "").strip() or f"import: {len(valid)} leads"
    row = SearchHistory(
        username=user.username,
        origin=origin,
        actor=actor,
        query=_clip_history_query(label),
        entity_type="people",
        page=1,
        per_page=_UI_PER_PAGE,
        total_results=len(valid),
        results_json=orjson.dumps(page_records, default=str).decode(),
        search_params_json=orjson.dumps(
            {
                "source": "csv_import",
                "requested": len(ordered),
                "valid": len(valid),
                "dropped": len(ordered) - len(valid),
            }
        ).decode(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _append_unique_mongo_ids(
        db, search=row, mongo_ids=valid, seen=set(), next_position=0
    )
    await db.commit()
    await db.refresh(row)
    await publish_job(
        job_id=f"imp-{row.id}",
        job_type="import_search",
        ctx=JobContext(user=user.username, origin=origin, actor=actor, search_id=row.id),
        status=JobStatus.COMPLETED,
        progress=1.0,
        exit_status="ok",
        meta={"kind": "csv_import", "total": len(valid)},
    )
    return SimilaritySearchResponse(
        query=label,
        results=[],
        history=_to_detail(row, page_records),
    )


@router.get("/searches/{search_id}/export.csv")
async def export_search_csv(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> StreamingResponse:
    """Export every stored result for a search as CSV (name, email). Cross-user
    read (principle 1)."""
    row = await _get_search_any(db, search_id)

    mongo_ids = await _load_all_mongo_ids(db, search_id=search_id)
    if not mongo_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This search has no stored results to export",
        )

    client = LeadsClient(settings, token)
    filename = _csv_export_filename(row)
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename)}'
        ),
    }
    return StreamingResponse(
        _iter_search_csv(client, mongo_ids),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.get("/apollo/credits")
async def apollo_credits(
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> dict[str, Any]:
    """Proxy Apollo credit balance via leads GET /api/leads/apollo/api/v1/users/api_profile."""
    client = LeadsClient(settings, token)
    return await client.get_apollo_credits()


def _similarity_filter_label(
    *,
    company_id: str | None,
    company_ids: list[str] | None = None,
    entity_type: str | None,
    email_exists: bool | None,
    phone_exists: bool | None,
    linkedin_exists: bool | None,
) -> str:
    """Human-readable history label for a pure-filter (no-query) similarity search."""
    parts: list[str] = []
    if (company_id or "").strip():
        parts.append(f"company={company_id.strip()}")
    if company_ids:
        parts.append(f"companies={len(company_ids)}")
    if entity_type:
        parts.append(f"type={entity_type}")
    for name, flag in (
        ("email", email_exists),
        ("phone", phone_exists),
        ("linkedin", linkedin_exists),
    ):
        if flag is True:
            parts.append(f"has {name}")
        elif flag is False:
            parts.append(f"no {name}")
    return f"filter: {', '.join(parts)}" if parts else "filter"


async def _run_similarity_search(
    db: AsyncSession,
    client: LeadsClient,
    *,
    query: str | None,
    limit: int,
    username: str,
    origin: str,
    actor: str,
    embeds: list[str] | None = None,
    company_id: str | None = None,
    company_ids: list[str] | None = None,
    entity_type: str | None = None,
    email_exists: bool | None = None,
    phone_exists: bool | None = None,
    linkedin_exists: bool | None = None,
) -> tuple[SimilaritySearchResponse, SearchHistory]:
    """Milvus similarity search + persist a (cross-user-visible) history row.

    Shared by the UI ``/similarity-search`` endpoint and the MCP semantic-search
    endpoint. Attributes the row to ``username``/``origin``/``actor`` and emits a
    terminal ``semantic_search`` job event so the run is visible in the jobs
    service (semantic search is synchronous — there is no leads stream to pause).

    v2 (additive): forwards the optional embed-subset + filter params to leads and
    records them in ``search_params_json``. When ``query`` is empty (a pure-filter
    search, ``embeds == []``) the history label is derived from the filters.

    This helper is the **authoritative** gate: the schema validators are the
    user-facing 422 layer, but a non-schema caller (a future internal call) still
    cannot run an unbounded pure-filter list — both rules (query required when
    embeds is non-empty; at least one filter when ``embeds == []``) are enforced
    here too, as a 400."""
    query = (query or "").strip()
    company_id = (company_id or "").strip() or None
    company_ids = [v.strip() for v in (company_ids or []) if (v or "").strip()] or None
    effective_embeds = embeds if embeds is not None else ["apollo"]
    if effective_embeds:
        if not query:
            raise HTTPException(
                status_code=400, detail="query is required when embeds is non-empty"
            )
    else:
        # Pure-filter run: the query text is never used, and at least one filter is
        # mandatory so this can never degrade into an unbounded list.
        query = ""
        has_filter = any(
            value is not None
            for value in (
                company_id,
                company_ids,
                entity_type,
                email_exists,
                phone_exists,
                linkedin_exists,
            )
        )
        if not has_filter:
            raise HTTPException(
                status_code=400,
                detail="a pure filter search (embeds == []) requires at least one filter",
            )

    try:
        hits = await client.similarity_search(
            query or None,
            limit=limit,
            embeds=embeds,
            company_id=company_id,
            company_ids=company_ids,
            entity_type=entity_type,
            email_exists=email_exists,
            phone_exists=phone_exists,
            linkedin_exists=linkedin_exists,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Similarity search failed: {exc}",
        ) from exc

    scored: list[SimilarityHitOut] = []
    records: list[dict[str, Any]] = []
    for hit in hits:
        lead = hit.get("lead")
        if not isinstance(lead, dict):
            continue
        record = lead_to_record(lead)
        if not record:
            continue
        raw_score = hit.get("score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else None
        scored.append(SimilarityHitOut(score=score, record=record))
        records.append(record)

    per_page = _UI_PER_PAGE
    page_results = records[:per_page]
    if query:
        history_query = _clip_history_query(query)
    else:
        history_query = _clip_history_query(
            _similarity_filter_label(
                company_id=company_id,
                company_ids=company_ids,
                entity_type=entity_type,
                email_exists=email_exists,
                phone_exists=phone_exists,
                linkedin_exists=linkedin_exists,
            )
        )
    search_params = {
        "source": "similarity",
        "query": query,
        "limit": limit,
        # History-row entity_type is always "people" (the search-history taxonomy);
        # the semantic *filter* entity_type is recorded separately below.
        "entity_type": "people",
        "embeds": embeds,
        "company_id": company_id,
        "company_ids": company_ids,
        "entity_type_filter": entity_type,
        "email_exists": email_exists,
        "phone_exists": phone_exists,
        "linkedin_exists": linkedin_exists,
    }
    row = SearchHistory(
        username=username,
        origin=origin,
        actor=actor,
        query=history_query,
        entity_type="people",
        page=1,
        per_page=per_page,
        total_results=len(records),
        results_json=orjson.dumps(page_results, default=str).decode(),
        search_params_json=json.dumps(search_params),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    mongo_ids = [
        str(record.get("mongo_id") or "").strip()
        for record in records
        if str(record.get("mongo_id") or "").strip()
    ]
    await _append_unique_mongo_ids(
        db,
        search=row,
        mongo_ids=mongo_ids,
        seen=set(),
        next_position=0,
    )
    await db.commit()
    await db.refresh(row)

    await publish_job(
        job_id=f"sem-{row.id}",
        job_type="semantic_search",
        ctx=JobContext(
            user=username, origin=origin, actor=actor, search_id=row.id
        ),
        status=JobStatus.COMPLETED,
        progress=1.0,
        exit_status="ok",
        meta={"kind": "semantic", "total": len(records)},
    )

    response = SimilaritySearchResponse(
        query=query,
        results=scored[:per_page],
        history=_to_detail(row, page_results),
    )
    return response, row


@router.post("/similarity-search", response_model=SimilaritySearchResponse)
async def similarity_search(
    body: SimilaritySearchRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> SimilaritySearchResponse:
    """Proxy Milvus similarity search; persist history and paginate at 100/page."""
    client = LeadsClient(settings, token)
    origin, actor = _principal_attribution()
    response, _row = await _run_similarity_search(
        db,
        client,
        query=body.query,
        limit=body.limit,
        username=user.username,
        origin=origin,
        actor=actor,
        embeds=body.embeds,
        company_id=body.company_id,
        company_ids=body.company_ids,
        entity_type=body.entity_type,
        email_exists=body.email_exists,
        phone_exists=body.phone_exists,
        linkedin_exists=body.linkedin_exists,
    )
    return response


@router.get("/companies/recent")
async def recent_companies(
    limit: int = Query(default=25, ge=1, le=100),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Recently ingested/updated companies (slim) — feeds the company-filter picker."""
    client = LeadsClient(settings, token)
    leads = await client.recent_leads(entity_type="organization", limit=limit)
    out: list[dict[str, Any]] = []
    for lead in leads:
        record = lead_to_record(lead)
        if not record:
            continue
        out.append(
            {
                "mongo_id": record.get("mongo_id"),
                "apollo_id": lead.get("apollo_id"),
                "name": record.get("name"),
            }
        )
    return out


@router.get("/companies/resolve")
async def resolve_company(
    value: str = Query(min_length=1),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> dict[str, Any]:
    """Resolve a company record id or Apollo org id to {mongo_id, apollo_id, name}."""
    client = LeadsClient(settings, token)
    lead = await client.resolve_organization_value(value)
    record = lead_to_record(lead) or {}
    return {
        "mongo_id": record.get("mongo_id"),
        "apollo_id": lead.get("apollo_id"),
        "name": record.get("name"),
    }


@router.get("/leads/{mongo_id}")
async def get_lead(
    mongo_id: str,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> dict[str, Any]:
    """Re-hydrate a lead from Mongo by `_id` (e.g. after async webhook enrich)."""
    client = LeadsClient(settings, token)
    # RecordDetail refresh: keep the FULL apollo_responses (the raw-response
    # viewer renders every stored Apollo endpoint), so opt out of display slimming.
    leads = await client.get_by_mongo_ids([mongo_id], fields="full")
    if not leads:
        raise HTTPException(status_code=404, detail="Lead not found")
    record = lead_to_record(leads[0])
    if not record:
        raise HTTPException(status_code=502, detail="Lead could not be normalized")
    return record


def _enrich_ids_from_request(
    apollo_id: str | None,
    body: dict[str, Any] | None,
) -> list[str]:
    payload = dict(body or {})
    ids: list[str] = []
    raw_ids = payload.get("ids")
    if isinstance(raw_ids, list):
        ids.extend(str(item).strip() for item in raw_ids if str(item or "").strip())
    for key in ("id", "person_id", "organization_id", "apollo_id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            ids.append(str(value).strip())
    if apollo_id and str(apollo_id).strip():
        ids.append(str(apollo_id).strip())
    return list(dict.fromkeys(ids))


async def _enrich_ndjson_events(
    client: LeadsClient,
    *,
    apollo_ids: list[str],
    start_stream,
) -> AsyncIterator[str]:
    """Relay a per-person leads stream job (complete-info or people/match).

    ``start_stream(apollo_id)`` must return ``{ingest_stream_id, embedding_stream_id}``
    — same shape for enrich and match. Event relay is identical for both.

    Starts the next person's ingest as soon as the previous ingest completes;
    embedding streams keep running in parallel. Every progress event includes
    ``active_stream_ids`` so the UI can cancel the whole in-flight batch.
    """
    total = max(len(apollo_ids), 1)
    line_q: asyncio.Queue[str | None] = asyncio.Queue()
    # Tracked per-kind so the UI can cancel fetching and embedding independently.
    active_ingest_ids: set[str] = set()
    active_embed_ids: set[str] = set()
    state_lock = asyncio.Lock()
    embed_finished = 0
    ingest_finished = 0
    pending_phone = 0
    pending_waterfall = 0

    async def _emit(payload: dict[str, Any]) -> None:
        async with state_lock:
            active_ingest = sorted(active_ingest_ids)
            active_embed = sorted(active_embed_ids)
            phone_n = pending_phone
            waterfall_n = pending_waterfall
        await line_q.put(
            _ndjson_line(
                {
                    **payload,
                    "active_ingest_stream_ids": active_ingest,
                    "active_embedding_stream_ids": active_embed,
                    # Union kept for backward compatibility with older clients.
                    "active_stream_ids": sorted({*active_ingest, *active_embed}),
                    "phone_reveal_pending_count": phone_n,
                    "waterfall_pending_count": waterfall_n,
                }
            )
        )

    await _emit(
        {
            "type": "progress",
            "kind": "ingest",
            "page": 0,
            "total_pages": total,
            "stored": 0,
            "ids": [],
            "ingest_stream_id": None,
            "embedding_stream_id": None,
        }
    )

    async def _watch_person(
        index: int, apollo_id: str
    ) -> tuple[asyncio.Event, asyncio.Task[None]]:
        """Start streams for one person; Event fires when ingest completes."""
        ingest_done_event = asyncio.Event()

        handles = await start_stream(apollo_id)
        ingest_stream_id = str(handles.get("ingest_stream_id") or "").strip()
        embedding_stream_id = str(handles.get("embedding_stream_id") or "").strip() or None
        if not ingest_stream_id:
            raise HTTPException(status_code=502, detail="Leads stream missing ingest_stream_id")

        async with state_lock:
            active_ingest_ids.add(ingest_stream_id)
            if embedding_stream_id:
                active_embed_ids.add(embedding_stream_id)

        subscribe_ids = [ingest_stream_id]
        if embedding_stream_id:
            subscribe_ids.append(embedding_stream_id)

        async def _consume() -> None:
            nonlocal embed_finished, ingest_finished, pending_phone, pending_waterfall
            ingest_done = False
            embedding_done = embedding_stream_id is None
            if embedding_done:
                async with state_lock:
                    embed_finished += 1
            try:
                async for event in client.subscribe_streams(subscribe_ids):
                    event_stream_id = str(event.get("stream_id") or "").strip()
                    event_kind = str(event.get("kind") or "").strip()
                    if not event_kind:
                        event_kind = (
                            "embedding"
                            if embedding_stream_id and event_stream_id == embedding_stream_id
                            else "ingest"
                        )
                    event_type = event.get("type")

                    if event_kind == "embedding":
                        if event_type == "progress":
                            event_done = int(event.get("done") or 0)
                            event_total = int(event.get("total") or 0)
                            # Ignore create_embedding_stream's empty 0/0 tick. Opening
                            # the circle from that makes email/match sit at 0% for the
                            # whole Apollo call before any embed work starts.
                            if event_done == 0 and event_total == 0:
                                continue
                            async with state_lock:
                                local_done = 1 if event_done > 0 else 0
                                done_count = min(total, embed_finished + local_done)
                                in_flight = event_total > event_done
                            await _emit(
                                {
                                    "type": "embedding_progress",
                                    "kind": "embedding",
                                    "done": done_count,
                                    "total": total,
                                    "in_flight": in_flight,
                                    "embedding_stream_id": embedding_stream_id,
                                }
                            )
                        elif event_type == "complete":
                            embedding_done = True
                            async with state_lock:
                                embed_finished += 1
                                done_count = embed_finished
                                batch_done = done_count >= total
                                if embedding_stream_id:
                                    active_embed_ids.discard(embedding_stream_id)
                            await _emit(
                                {
                                    "type": "embedding_progress",
                                    "kind": "embedding",
                                    "done": done_count,
                                    "total": total,
                                    # Only the final person clears the ring; earlier
                                    # completions just advance the determinate %.
                                    "complete": batch_done,
                                    "embedding_stream_id": embedding_stream_id,
                                }
                            )
                        elif event_type == "error":
                            embedding_done = True
                            async with state_lock:
                                embed_finished += 1
                                done_count = embed_finished
                                if embedding_stream_id:
                                    active_embed_ids.discard(embedding_stream_id)
                            await _emit(
                                {
                                    "type": "embedding_progress",
                                    "kind": "embedding",
                                    "done": done_count,
                                    "total": total,
                                    "error": str(event.get("detail") or "Embedding failed"),
                                    "embedding_stream_id": embedding_stream_id,
                                }
                            )
                    else:
                        if event_type == "ids":
                            raw_ids = event.get("ids") or []
                            mongo_ids = [
                                str(item).strip()
                                for item in raw_ids
                                if str(item or "").strip()
                            ]
                            async with state_lock:
                                if event.get("phone_reveal_pending"):
                                    pending_phone += 1
                                if event.get("waterfall_pending"):
                                    pending_waterfall += 1
                            await _emit(
                                {
                                    "type": "progress",
                                    "kind": "ingest",
                                    "page": index,
                                    "total_pages": total,
                                    "stored": index,
                                    "ids": mongo_ids,
                                    "ingest_stream_id": ingest_stream_id,
                                    "embedding_stream_id": embedding_stream_id,
                                    "phone_reveal_pending": bool(
                                        event.get("phone_reveal_pending")
                                    ),
                                    "waterfall_pending": bool(event.get("waterfall_pending")),
                                }
                            )
                        elif event_type == "item_error":
                            # Per-person failure on the leads side; the stream
                            # continues (a "complete" event still follows).
                            await _emit(
                                {
                                    "type": "item_error",
                                    "kind": "ingest",
                                    "page": index,
                                    "total_pages": total,
                                    "detail": str(event.get("detail") or "Enrich failed"),
                                }
                            )
                        elif event_type == "error":
                            detail = event.get("detail") or "Leads stream failed"
                            async with state_lock:
                                if not embedding_done and embedding_stream_id:
                                    embed_finished += 1
                                    embedding_done = True
                            ingest_done_event.set()
                            await line_q.put(
                                _ndjson_line({"type": "error", "detail": detail})
                            )
                            return
                        elif event_type == "complete":
                            ingest_done = True
                            async with state_lock:
                                ingest_finished += 1
                                stored = ingest_finished
                                active_ingest_ids.discard(ingest_stream_id)
                            ingest_done_event.set()
                            await _emit(
                                {
                                    "type": "ingest_complete",
                                    "kind": "ingest",
                                    "page": stored,
                                    "total_pages": total,
                                    "stored": stored,
                                    "ingest_stream_id": ingest_stream_id,
                                    "embedding_stream_id": embedding_stream_id,
                                    "complete": stored >= total,
                                }
                            )

                    if ingest_done and embedding_done:
                        return
            except Exception as exc:
                # A subscribe-level failure (e.g. leads 401/403 now that the
                # leads backend authorizes every request) must surface as an
                # error line — otherwise the batch's gather() swallows it and
                # the finally below emits a false "complete".
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                await line_q.put(_ndjson_line({"type": "error", "detail": detail}))
            finally:
                if not ingest_done_event.is_set():
                    ingest_done_event.set()

        task = asyncio.create_task(
            _consume(),
            name=f"enrich-watch-{index}-{apollo_id[:8]}",
        )
        return ingest_done_event, task

    async def _run_batch() -> None:
        consume_tasks: list[asyncio.Task[None]] = []
        try:
            for index, apollo_id in enumerate(apollo_ids, start=1):
                ingest_done_event, task = await _watch_person(index, apollo_id)
                consume_tasks.append(task)
                await ingest_done_event.wait()
            if consume_tasks:
                await asyncio.gather(*consume_tasks, return_exceptions=True)
        except Exception as exc:
            await line_q.put(_ndjson_line({"type": "error", "detail": str(exc)}))
        finally:
            for task in consume_tasks:
                if not task.done():
                    task.cancel()
            if consume_tasks:
                await asyncio.gather(*consume_tasks, return_exceptions=True)
            await line_q.put(_ndjson_line({"type": "complete"}))
            await line_q.put(None)

    runner = asyncio.create_task(_run_batch(), name="enrich-batch-runner")
    try:
        while True:
            line = await line_q.get()
            if line is None:
                break
            yield line
            if '"type": "error"' in line:
                break
    finally:
        if not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        async with state_lock:
            leftover = list({*active_ingest_ids, *active_embed_ids})
        for stream_id in leftover:
            try:
                await client.cancel_stream(stream_id)
            except Exception:
                pass


@router.post("/people/enrich")
@router.post("/people/enrich/{apollo_id}")
async def enrich_person(
    request: Request,
    apollo_id: str | None = None,
    body: dict[str, Any] | None = Body(default=None),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> Any:
    """Proxy person enrichment to leads.

    With ``stream=true`` (body/query) or ``Accept: application/x-ndjson``, returns an
    NDJSON progress stream (retrieve + embedding), same style as ``POST /api/search``.
    """
    client = LeadsClient(settings, token)
    payload = dict(body or {})
    stream = bool(payload.get("stream"))
    if not stream:
        stream = "application/x-ndjson" in (request.headers.get("accept") or "").lower()
    ids = _enrich_ids_from_request(apollo_id, payload)
    if stream:
        if not ids:
            raise HTTPException(status_code=400, detail="At least one apollo id is required")

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for line in _enrich_ndjson_events(
                    client,
                    apollo_ids=ids,
                    start_stream=client.start_person_enrich_stream,
                ):
                    yield line
            except HTTPException as exc:
                yield _ndjson_line({"type": "error", "detail": exc.detail})
            except Exception as exc:
                # Same never-raise invariant: transport/DB errors mid-stream must
                # surface as an error event, not a connection reset.
                logger.exception("NDJSON stream failed")
                yield _ndjson_line({"type": "error", "detail": f"Stream failed: {exc}"})

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Leads returns SearchIdsOut; hydrate mongo ids for the UI record.
    result = await client.enrich_person(apollo_id, payload)
    mongo_ids = result.get("ids") if isinstance(result, dict) else None
    if not isinstance(mongo_ids, list) or not mongo_ids:
        raise HTTPException(status_code=502, detail="Leads enrich returned no ids")
    # Enrich/match responses are a full-fidelity contract (P2): the caller just
    # paid to reveal data — never slim these single-doc hydrations.
    leads = await client.get_by_mongo_ids([str(mongo_ids[0])], fields="full")
    if not leads:
        raise HTTPException(status_code=404, detail="Enriched lead not found")
    record = lead_to_record(leads[0])
    if not record:
        raise HTTPException(status_code=502, detail="Enriched lead could not be normalized")
    return record


@router.post("/people/match")
@router.post("/people/match/{apollo_id}")
async def match_person(
    request: Request,
    apollo_id: str | None = None,
    body: dict[str, Any] | None = Body(default=None),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> Any:
    """Proxy people/match (waterfall email/phone). Same NDJSON shape as enrich when streaming."""
    client = LeadsClient(settings, token)
    payload = dict(body or {})
    stream = bool(payload.get("stream"))
    if not stream:
        stream = "application/x-ndjson" in (request.headers.get("accept") or "").lower()
    ids = _enrich_ids_from_request(apollo_id, payload)
    match_params = {
        key: payload[key]
        for key in (
            "run_waterfall_email",
            "run_waterfall_phone",
            "reveal_phone_number",
        )
        if key in payload
    }
    if stream:
        if not ids:
            raise HTTPException(status_code=400, detail="At least one apollo id is required")

        async def start_match(person_id: str) -> dict[str, str | None]:
            return await client.start_person_match_stream(person_id, match_params)

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for line in _enrich_ndjson_events(
                    client,
                    apollo_ids=ids,
                    start_stream=start_match,
                ):
                    yield line
            except HTTPException as exc:
                yield _ndjson_line({"type": "error", "detail": exc.detail})
            except Exception as exc:
                # Same never-raise invariant: transport/DB errors mid-stream must
                # surface as an error event, not a connection reset.
                logger.exception("NDJSON stream failed")
                yield _ndjson_line({"type": "error", "detail": f"Stream failed: {exc}"})

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Leads returns SearchIdsOut; hydrate mongo ids for the UI record.
    result = await client.match_person(apollo_id, {**payload, **match_params})
    mongo_ids = result.get("ids") if isinstance(result, dict) else None
    if not isinstance(mongo_ids, list) or not mongo_ids:
        raise HTTPException(status_code=502, detail="Leads match returned no ids")
    # Enrich/match responses are a full-fidelity contract (P2): the caller just
    # paid to reveal data — never slim these single-doc hydrations.
    leads = await client.get_by_mongo_ids([str(mongo_ids[0])], fields="full")
    if not leads:
        raise HTTPException(status_code=404, detail="Matched lead not found")
    record = lead_to_record(leads[0])
    if not record:
        raise HTTPException(status_code=502, detail="Matched lead could not be normalized")
    return {
        "lead": record,
        "phone_reveal_pending": bool(match_params.get("reveal_phone_number"))
        or bool(match_params.get("run_waterfall_phone")),
        "waterfall_pending": bool(match_params.get("run_waterfall_email"))
        or bool(match_params.get("run_waterfall_phone")),
        "webhook_url": None,
        "raw_lead": leads[0],
    }


@router.post("/organizations/enrich")
@router.post("/organizations/enrich/{apollo_id}")
async def enrich_organization(
    request: Request,
    apollo_id: str | None = None,
    body: dict[str, Any] | None = Body(default=None),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> Any:
    """Proxy organization enrichment to leads (optional NDJSON stream like people enrich)."""
    client = LeadsClient(settings, token)
    payload = dict(body or {})
    stream = bool(payload.get("stream"))
    if not stream:
        stream = "application/x-ndjson" in (request.headers.get("accept") or "").lower()
    ids = _enrich_ids_from_request(apollo_id, payload)
    if stream:
        if not ids:
            raise HTTPException(status_code=400, detail="At least one apollo id is required")

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for line in _enrich_ndjson_events(
                    client,
                    apollo_ids=ids,
                    start_stream=client.start_organization_enrich_stream,
                ):
                    yield line
            except HTTPException as exc:
                yield _ndjson_line({"type": "error", "detail": exc.detail})
            except Exception as exc:
                # Same never-raise invariant: transport/DB errors mid-stream must
                # surface as an error event, not a connection reset.
                logger.exception("NDJSON stream failed")
                yield _ndjson_line({"type": "error", "detail": f"Stream failed: {exc}"})

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result = await client.enrich_organization(apollo_id, payload)
    mongo_ids = result.get("ids") if isinstance(result, dict) else None
    if not isinstance(mongo_ids, list) or not mongo_ids:
        raise HTTPException(status_code=502, detail="Leads enrich returned no ids")
    # Enrich/match responses are a full-fidelity contract (P2): the caller just
    # paid to reveal data — never slim these single-doc hydrations.
    leads = await client.get_by_mongo_ids([str(mongo_ids[0])], fields="full")
    if not leads:
        raise HTTPException(status_code=404, detail="Enriched lead not found")
    record = lead_to_record(leads[0])
    if not record:
        raise HTTPException(status_code=502, detail="Enriched lead could not be normalized")
    return record
