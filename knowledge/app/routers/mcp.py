"""The MCP-consumed surface: /api/knowledge/mcp/v1/* (P2: versioned, additive).

Two answer modes, deliberately separate endpoints (the plan's load-bearing
principle): /query/records is deterministic (schema-validated filters +
similarity thresholds over the records store, provenance on every row);
/search is semantic recall over the Graphiti graphs. /remember and the
user scope of /search are the per-user session-context surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fm_runtime import Principal, confirmation_threshold, require_confirmation, require_principal
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.database import Session
from app.embeddings import embeddings_available
from app.graph import graph_service, user_group_id
from app.models import SyncState
from app.query import run_query
from app.schema_registry import RECORD_TYPES, describe
from app.sync import sync_manager

router = APIRouter(prefix="/api/knowledge/mcp/v1", tags=["mcp"])


async def _record_counts() -> dict[str, int]:
    async with Session() as session:
        return {
            name: int((await session.execute(select(func.count()).select_from(rt.model))).scalar_one())
            for name, rt in RECORD_TYPES.items()
        }


# ------------------------------------------------------------------ schema --
@router.get("/schema")
async def get_schema(_: Principal = Depends(require_principal)) -> dict[str, Any]:
    cfg = get_settings()
    out = describe(cfg.fuzzy_threshold_default)
    out["limits"] = {"default": cfg.query_limit_default, "max": cfg.query_limit_max}
    out["fuzzy"]["available"] = embeddings_available()
    out["row_counts"] = await _record_counts()
    return out


# ----------------------------------------------------------- query_records --
class QueryRecordsRequest(BaseModel):
    record_type: str
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int | None = Field(default=None, ge=1)


@router.post("/query/records")
async def query_records(
    body: QueryRecordsRequest, _: Principal = Depends(require_principal)
) -> dict[str, Any]:
    async with Session() as session:
        return await run_query(session, body.record_type, body.filters, body.limit)


# ------------------------------------------------------------------ search --
class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=2000)
    scope: str = Field(default="app", pattern="^(app|user|all)$")
    mode: str = Field(default="search", pattern="^(search|recent)$")
    limit: int = Field(default=20, ge=1, le=100)


@router.post("/search")
async def search(body: SearchRequest, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    try:
        if body.mode == "recent":
            items = await graph_service.recent_episodes(
                scope=body.scope, username=principal.username, limit=body.limit
            )
            return {"mode": "recent", "scope": body.scope, "episodes": items}
        if not body.query.strip():
            raise HTTPException(status_code=422, detail="mode=search needs a non-empty query")
        facts = await graph_service.search(
            query=body.query, scope=body.scope, username=principal.username, limit=body.limit
        )
        return {"mode": "search", "scope": body.scope, "facts": facts}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"knowledge graph unavailable: {exc}") from exc


# ---------------------------------------------------------------- remember --
class RememberRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


@router.post("/remember")
async def remember(body: RememberRequest, principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Store session context in the calling user's own graph (cross-session
    memory; user-scoped by design — an approved P1 carve-out)."""
    now = datetime.now(timezone.utc)
    try:
        await graph_service.add_episode(
            group_id=user_group_id(principal.username),
            name=f"remember-{now.strftime('%Y%m%dT%H%M%S')}",
            body=body.content,
            source_description=f"session context stored via knowledge_remember by {principal.username}",
            reference_time=now,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"knowledge graph unavailable: {exc}") from exc
    return {"stored": True, "group": "user", "owner": principal.username}


# -------------------------------------------------------------------- sync --
class SyncRequest(BaseModel):
    sources: list[str] | None = None
    confirm: bool = False
    confirm_token: str | None = None
    human_approval: str | None = None


@router.post("/sync")
async def sync(body: SyncRequest, _: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Immediate sync. Records+embedding passes always run (cheap). The graph
    pass is P4-gated: over-threshold backlogs 409 with an estimate until
    confirmed — and agent-origin callers need a human_approval token."""
    names = body.sources or ["searches", "mail_campaigns", "mail_messages", "agent_sessions"]
    unknown = [n for n in names if n not in ("searches", "mail_campaigns", "mail_messages", "agent_sessions")]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown sources: {unknown}")

    # Records + embeddings first (cheap, idempotent), so the graph estimate
    # below reflects reality including just-arrived rows.
    summary = await sync_manager.run_now(names, graph_batch=0, auto=False)

    pending = await sync_manager.graph_pending(names)
    estimate = float(sum(pending.values()))
    threshold = confirmation_threshold("KNOWLEDGE_SYNC_CONFIRM_EPISODES", get_settings().sync_confirm_episodes)
    if estimate > 0:
        require_confirmation(
            estimate,
            threshold,
            confirm=body.confirm,
            confirm_token=body.confirm_token,
            human_approval=body.human_approval,
            unit="graph episodes (LLM extraction)",
            action="knowledge_sync_graph",
            message=(
                f"Graph ingestion will distill {int(estimate)} records into Graphiti episodes "
                "(multiple LLM calls each). Re-invoke with confirm=true"
            ),
            meta={"pending": pending},
        )
        # records=False: the sources were polled seconds ago by the first pass —
        # this phase does only the (now-confirmed) graph + session-graph work.
        graph_summary = await sync_manager.run_now(
            names, graph_batch=int(estimate), auto=False, records=False
        )
        summary["graph"] = graph_summary.get("graph")
        if "session_graphs" in graph_summary:
            summary["session_graphs"] = graph_summary["session_graphs"]
    return {"synced": names, "summary": summary}


# ------------------------------------------------------------------- stats --
@router.get("/stats")
async def stats(_: Principal = Depends(require_principal)) -> dict[str, Any]:
    cfg = get_settings()
    counts = await _record_counts()
    async with Session() as session:
        states = (await session.execute(select(SyncState))).scalars().all()
        sync_states = {
            s.source: {
                "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
                "last_new": s.last_new,
                "last_error": s.last_error or None,
                "duration_ms": s.duration_ms,
            }
            for s in states
        }
    return {
        "records": counts,
        "sync": sync_states,
        "last_cycle": sync_manager.last_cycle,
        "graph": {
            "available": graph_service.available,
            "reason": None if graph_service.available else graph_service.unavailable_reason,
            "ingest_sources": sorted(cfg.graph_source_set),
            "pending_episodes": await sync_manager.graph_pending(),
            "session_ingest": cfg.session_ingest_enabled,
        },
        "fuzzy": {"available": embeddings_available(), "default_threshold": cfg.fuzzy_threshold_default},
        "models": {
            "llm": cfg.knowledge_llm_model,
            "small": cfg.knowledge_small_llm_model,
            "embedding": cfg.openai_embedding_model,
        },
    }
