"""MCP-facing search API — ``/api/search/mcp/v1/*``.

Deliberately **distinct handlers from the UI routes** (principle 2: MCP uses
dedicated endpoints, never the ones the browser calls) and **versioned**
(``/v1``, additive-only within the version). Callable by the ``mcp`` service via
the ``mcp->search`` exchange edge; the acting human stays the token subject, so
records persist with the correct owner/origin/actor and are cross-user visible.

Every Apollo-touching path still funnels through ``LeadsClient`` -> leads -> Apollo
— this surface never holds an Apollo key or calls Apollo directly. Searches
started here run detached and surface in the jobs service (search is the job
producer); this endpoint returns the handles synchronously so the agent can watch
progress there.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, oauth2_scheme
from app.config import Settings, get_settings
from app.database import get_db
from app.leads_client import LeadsClient, lead_to_record
from app.mail_client import MailClient
from app.models import SearchHistory
from app.routers.search import (
    _UI_PER_PAGE,
    _build_search_params,
    _count_stored_results,
    _get_search_any,
    _history_label_for_company_search,
    _history_label_for_people_search,
    _hydrate_results,
    _load_all_mongo_ids,
    _load_page_mongo_ids,
    _principal_attribution,
    _record_company,
    _record_title,
    _resolved_record_email,
    _resolved_record_phone,
    _run_similarity_search,
    _start_search_job_detached,
    _to_detail,
)
from app.schemas import (
    McpApolloSearchRequest,
    McpEnrichRequest,
    McpExportRequest,
    McpExportResponse,
    McpExportRow,
    McpSearchStartResponse,
    McpSemanticSearchRequest,
    SearchHistoryDetail,
    SearchHistorySummary,
    SimilaritySearchResponse,
    UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search/mcp/v1", tags=["search-mcp"])

# Ceiling on how many results the export path will hydrate in one call — an MCP
# export of a 100k-result search would otherwise be an unbounded batch. The
# authoritative full export stays the streaming CSV endpoint.
_MCP_EXPORT_MAX = 5000


@router.post("/searches/apollo", response_model=McpSearchStartResponse)
async def mcp_start_apollo_search(
    body: McpApolloSearchRequest,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> McpSearchStartResponse:
    """Start an Apollo people/company search (via leads). Returns the created
    ``search_id`` + leads stream handles once ingest starts; the search continues
    detached and is watchable/controllable in the jobs service."""
    client = LeadsClient(settings, token)
    company_name = (body.company_name or "").strip() or None
    company_domain = (body.company_domain or "").strip() or None

    resolved: dict[str, Any] | None = None
    if body.entity_type == "people" and (body.organization_name or "").strip():
        resolved = await client.resolve_organization_by_name(
            body.organization_name.strip()
        )

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
    started = await _start_search_job_detached(
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
    return McpSearchStartResponse(
        search_id=int(started["search_id"]),
        entity_type=body.entity_type,
        query=history_query,
        ingest_stream_id=started.get("ingest_stream_id"),
        embedding_stream_id=started.get("embedding_stream_id"),
    )


@router.post("/searches/semantic", response_model=SimilaritySearchResponse)
async def mcp_start_semantic_search(
    body: McpSemanticSearchRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    user: UserOut = Depends(get_current_user),
) -> SimilaritySearchResponse:
    """Semantic (Milvus) search via leads; persists a cross-user-visible history
    row. Synchronous — no leads stream — but still recorded as a job event."""
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
        entity_type=body.entity_type,
        email_exists=body.email_exists,
        phone_exists=body.phone_exists,
        linkedin_exists=body.linkedin_exists,
    )
    return response


@router.post("/leads/enrich")
async def mcp_enrich_leads(
    body: McpEnrichRequest,
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> dict[str, Any]:
    """Enrich one or more leads by Apollo id (via leads -> Apollo). Returns the
    hydrated records + per-id errors; write op (mutates the leads store)."""
    ids = [str(i).strip() for i in body.ids if str(i or "").strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="At least one apollo id is required")

    match_params = {
        "run_waterfall_email": body.run_waterfall_email,
        "run_waterfall_phone": body.run_waterfall_phone,
        "reveal_phone_number": body.reveal_phone_number,
    }
    client = LeadsClient(settings, token)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for apollo_id in ids:
        try:
            if body.kind == "match":
                result = await client.match_person(apollo_id, dict(match_params))
            elif body.kind == "organization":
                result = await client.enrich_organization(apollo_id)
            else:
                result = await client.enrich_person(apollo_id)
            mongo_ids = result.get("ids") if isinstance(result, dict) else None
            if not isinstance(mongo_ids, list) or not mongo_ids:
                errors.append({"id": apollo_id, "detail": "leads returned no ids"})
                continue
            # MCP enrich tool responses are a stable full-fidelity contract (P2).
            leads = await client.get_by_mongo_ids([str(mongo_ids[0])], fields="full")
            record = lead_to_record(leads[0]) if leads else None
            if record:
                records.append(record)
            else:
                errors.append({"id": apollo_id, "detail": "enriched lead not found"})
        except HTTPException as exc:
            errors.append({"id": apollo_id, "detail": str(exc.detail)})
    return {"kind": body.kind, "records": records, "errors": errors}


@router.get("/searches", response_model=list[SearchHistorySummary])
async def mcp_list_searches(
    username: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: UserOut = Depends(get_current_user),
) -> list[Any]:
    """List searches (cross-user; ``?username=`` narrows to one owner)."""
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


@router.get("/searches/{search_id}", response_model=SearchHistoryDetail)
async def mcp_get_search(
    search_id: int,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> SearchHistoryDetail:
    """Get one search + its current page of hydrated results (cross-user)."""
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


@router.get("/searches/{search_id}/results", response_model=list[McpExportRow])
async def mcp_list_results(
    search_id: int,
    limit: int = 200,
    offset: int = 0,
    exclude_contacted: bool = False,
    campaign_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> list[McpExportRow]:
    """List a page of a search's stored results as compact records (cross-user).

    ``exclude_contacted`` drops leads already messaged (via the search->mail hop),
    optionally scoped to one ``campaign_id`` (omit for all campaigns). It filters
    the hydrated page, so a filtered page may return fewer than ``limit`` rows; if
    mail can't be consulted the filter is a graceful no-op (nothing dropped). The
    authoritative dedupe still happens at send time in mail."""
    await _get_search_any(db, search_id)
    limit = max(1, min(int(limit), _MCP_EXPORT_MAX))
    offset = max(0, int(offset))
    all_ids = await _load_all_mongo_ids(db, search_id=search_id)
    window = all_ids[offset : offset + limit]
    client = LeadsClient(settings, token)
    records = await _hydrate_results(client, window)
    rows = [_export_row(record) for record in records]
    if exclude_contacted:
        rows, _excluded, _applied = await _filter_contacted(settings, rows, campaign_id)
    return rows


@router.post("/searches/{search_id}/export", response_model=McpExportResponse)
async def mcp_export_search(
    search_id: int,
    body: McpExportRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    token: str = Depends(oauth2_scheme),
    _: UserOut = Depends(get_current_user),
) -> McpExportResponse:
    """Export a search's stored results, optionally dropping already-contacted
    leads (via the search->mail hop, reading mail's contacted set). Scope with
    ``campaign_id`` (omit for all campaigns). If mail can't be consulted the filter
    is a graceful no-op — ``exclude_contacted_applied`` reports whether it actually
    ran. The authoritative dedupe still happens at send time in mail."""
    await _get_search_any(db, search_id)
    all_ids = await _load_all_mongo_ids(db, search_id=search_id)
    total = len(all_ids)
    window = all_ids[:_MCP_EXPORT_MAX]

    client = LeadsClient(settings, token)
    records = await _hydrate_results(client, window)
    rows = [_export_row(record) for record in records]

    excluded = 0
    applied = False
    if body.exclude_contacted:
        rows, excluded, applied = await _filter_contacted(
            settings, rows, body.campaign_id
        )

    return McpExportResponse(
        search_id=search_id,
        total=total,
        returned=len(rows),
        excluded_count=excluded,
        exclude_contacted_requested=body.exclude_contacted,
        exclude_contacted_applied=applied,
        results=rows,
    )


async def _filter_contacted(
    settings: Settings,
    rows: list[McpExportRow],
    campaign_id: str | None,
) -> tuple[list[McpExportRow], int, bool]:
    """Drop rows whose email is in mail's contacted set (search->mail hop).

    Returns ``(kept_rows, excluded_count, applied)``. ``applied`` is ``False``
    when mail could not be consulted (down / exchange refused): the filter then
    degrades to a graceful no-op and nothing is dropped, preserving the invariant
    that a search/export never fails because mail is unavailable."""
    contacted = await MailClient(settings).contacted_emails(campaign_id)
    if contacted is None:
        return rows, 0, False
    kept: list[McpExportRow] = []
    excluded = 0
    for row in rows:
        email = (row.email or "").strip().lower()
        if email and email in contacted:
            excluded += 1
            continue
        kept.append(row)
    return kept, excluded, True


def _export_row(record: dict[str, Any]) -> McpExportRow:
    return McpExportRow(
        mongo_id=str(record.get("mongo_id") or "").strip() or None,
        name=str(record.get("name") or "").strip(),
        email=_resolved_record_email(record),
        phone=_resolved_record_phone(record),
        company=_record_company(record),
        title=_record_title(record),
        entity_type=str(record.get("entity_type") or "person"),
    )
