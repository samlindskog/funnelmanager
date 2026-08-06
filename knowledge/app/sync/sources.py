"""The three source connectors: search history, mail (campaigns + archive
messages), and agent sessions. Each is watermark-incremental and idempotent
(upsert on the source's natural key)."""

from __future__ import annotations

from typing import Any

from fm_runtime import InternalClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    AgentSessionRecord,
    MailCampaignRecord,
    MailMessageRecord,
    SearchRecord,
)
from app.sync.base import (
    SyncResult,
    as_list,
    get_watermark,
    json_list,
    logger,
    parse_dt,
    split_address,
)


async def _get_json(base_url: str, audience: str, path: str, params: dict[str, Any] | None = None) -> Any:
    # follow_context=False pins the SERVICE identity (client credentials →
    # service account holding the machine-only `knowledge-internal` GET grants).
    # Humans deliberately have no mail/search/agents grants via knowledge-access,
    # so a request-context exchange would 403 a human-triggered /sync — and the
    # fan-out is the service's own job anyway (the jobs-control precedent).
    async with InternalClient(base_url, audience, timeout=30.0, follow_context=False) as client:
        resp = await client.get(path, params=params or {})
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------- searches --
async def sync_searches(session: AsyncSession) -> tuple[SyncResult, dict[str, Any]]:
    cfg = get_settings()
    result = SyncResult()
    payload = await _get_json(cfg.search_backend_url, "search", "/api/search/mcp/v1/searches")
    items = as_list(payload, "searches", "items")
    if len(items) >= 200:
        # The source endpoint hard-caps at 200 with no pagination — a burst
        # beyond that between cycles would be dropped. Surface it.
        logger.warning("searches: source returned the 200-row cap; older uncaptured rows may exist")
    wm = await get_watermark(session, "searches")
    seen_max = int(wm.get("max_id", 0))
    new_max = seen_max
    for item in items:
        sid = item.get("id")
        if not isinstance(sid, int):
            continue
        new_max = max(new_max, sid)
        existing = (
            await session.execute(select(SearchRecord).where(SearchRecord.source_id == sid))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                SearchRecord(
                    source_id=sid,
                    owner=str(item.get("username") or ""),
                    query=str(item.get("query") or ""),
                    entity_type=str(item.get("entity_type") or ""),
                    total_results=int(item.get("total_results") or 0),
                    occurred_at=parse_dt(item.get("created_at")),
                )
            )
            result.new += 1
        else:
            total = int(item.get("total_results") or 0)
            if existing.total_results != total:
                existing.total_results = total
                existing.graph_ingested_at = None  # re-distill the updated fact
                result.updated += 1
    wm["max_id"] = new_max
    await session.flush()
    return result, wm


# -------------------------------------------------------------- campaigns --
async def sync_mail_campaigns(session: AsyncSession) -> tuple[SyncResult, dict[str, Any]]:
    cfg = get_settings()
    result = SyncResult()
    payload = await _get_json(cfg.mail_backend_url, "mail", "/api/mail/mcp/v1/campaigns")
    items = as_list(payload, "campaigns", "items")
    if len(items) >= 500:
        logger.warning("mail_campaigns: source returned the 500-row cap; older uncaptured rows may exist")
    for item in items:
        cid = item.get("id")
        if not isinstance(cid, int):
            continue
        existing = (
            await session.execute(select(MailCampaignRecord).where(MailCampaignRecord.source_id == cid))
        ).scalar_one_or_none()
        fields = dict(
            owner=str(item.get("owner") or ""),
            name=str(item.get("name") or ""),
            subject=str(item.get("subject") or ""),
            status=str(item.get("status") or ""),
            send_strategy=str(item.get("send_strategy") or ""),
            occurred_at=parse_dt(item.get("created_at")),
        )
        if existing is None:
            session.add(MailCampaignRecord(source_id=cid, **fields))
            result.new += 1
        else:
            changed = False
            for k in ("name", "subject", "status", "send_strategy"):
                if getattr(existing, k) != fields[k]:
                    setattr(existing, k, fields[k])
                    changed = True
                    # A text change invalidates the stored embedding.
                    if k in ("name", "subject"):
                        setattr(existing, f"{k}_emb", None)
            if changed:
                # Re-distill: the graph episode must reflect the new status/text
                # (Graphiti's temporal logic invalidates the superseded facts).
                existing.graph_ingested_at = None
                result.updated += 1
    await session.flush()
    return result, {}


# --------------------------------------------------------------- messages --
async def sync_mail_messages(session: AsyncSession) -> tuple[SyncResult, dict[str, Any]]:
    """Sync every account's FULL archive (label=ALL — the source endpoint
    defaults to INBOX, which would silently drop all outbound/archived mail)."""
    cfg = get_settings()
    result = SyncResult()
    accounts_payload = await _get_json(cfg.mail_backend_url, "mail", "/api/mail/accounts")
    accounts = as_list(accounts_payload, "accounts", "items")
    wm = await get_watermark(session, "mail_messages")
    per_account: dict[str, Any] = wm.get("accounts", {})

    for account in accounts:
        aid = account.get("id")
        if not isinstance(aid, int):
            continue
        acct_email = str(account.get("email") or "").lower()
        owner = str(account.get("connected_by") or "")
        state = per_account.get(str(aid)) or {}
        if isinstance(state, str):  # legacy plain-marker shape
            state = {"marker": state}
        per_account[str(aid)] = await _walk_account(session, aid, acct_email, owner, dict(state), result)
    wm["accounts"] = per_account
    return result, wm


async def _walk_account(
    session: AsyncSession,
    aid: int,
    acct_email: str,
    owner: str,
    state: dict[str, Any],
    result: SyncResult,
) -> dict[str, Any]:
    """Newest-first walk of one account's archive; returns the new state.

    Two modes. INITIAL BACKFILL (no ``marker`` yet): resume from
    ``resume_page`` across cycles until the archive bottom, then record the
    head timestamp captured when the backfill started (``newest_candidate``)
    as the marker — new arrivals during the backfill sit above it and are
    caught by the first incremental walk. INCREMENTAL (``marker`` set): walk
    from page 1 until a message older than the marker (always at least
    ``mail_head_pages`` pages, catching label/deletion flips near the head).
    The marker only advances when a walk COMPLETES — a page-cap cut leaves it
    untouched, so a capped cycle never skips a gap (upserts are idempotent)."""
    cfg = get_settings()
    marker = str(state.get("marker") or "")
    backfilling = not marker
    page = int(state.get("resume_page") or 1) if backfilling else 1
    newest_seen = str(state.get("newest_candidate") or "")
    pages_this_cycle = 0
    completed = False

    while True:
        payload = await _get_json(
            cfg.mail_backend_url,
            "mail",
            "/api/mail/mcp/v1/messages",
            {
                "account_id": aid,
                "label": "ALL",
                "page": page,
                "per_page": cfg.sync_page_size,
                "include_deleted": "true",
            },
        )
        items = as_list(payload, "messages", "items")
        if not items:
            completed = True
            break
        past_watermark = False
        for item in items:
            gmail_id = str(item.get("gmail_id") or "")
            if not gmail_id:
                continue
            internal = str(item.get("internal_date") or "")
            if internal and internal > newest_seen:
                newest_seen = internal
            if marker and internal and internal < marker:
                past_watermark = True
            await _upsert_message(session, item, aid, acct_email, owner, result)
        await session.flush()
        if not backfilling and past_watermark and page >= cfg.mail_head_pages:
            completed = True
            break
        page += 1
        pages_this_cycle += 1
        if pages_this_cycle >= cfg.mail_pages_per_cycle:
            break

    if completed:
        return {"marker": newest_seen or marker}
    if backfilling:
        logger.info("mail_messages: backfill for account %s paused at page %s; resuming next cycle", aid, page)
        return {"resume_page": page, "newest_candidate": newest_seen}
    logger.warning("mail_messages: page cap hit mid-incremental for account %s — watermark held", aid)
    return {"marker": marker, "newest_candidate": newest_seen}


def parse_message_fields(item: dict[str, Any], account_email: str, owner: str) -> dict[str, Any]:
    """Pure transform: one mail-API message dict -> records-store columns.
    (Split out so direction/counterparty inference is unit-testable.)"""
    labels = {x.upper() for x in json_list(item.get("label_ids"))}
    from_name, from_email = split_address(str(item.get("from_addr") or ""))
    outbound = "SENT" in labels or (bool(from_email) and from_email == account_email)
    if outbound:
        tos = json_list(item.get("to_addrs"))
        cp_name, cp_email = split_address(tos[0] if tos else "")
    else:
        cp_name, cp_email = from_name, from_email
    return dict(
        thread_id=str(item.get("thread_id") or ""),
        account_email=account_email,
        owner=owner,
        direction="outbound" if outbound else "inbound",
        counterparty_email=cp_email,
        counterparty_name=cp_name,
        subject=str(item.get("subject") or ""),
        snippet=str(item.get("snippet") or ""),
        is_deleted=bool(item.get("is_deleted", False)),
        occurred_at=parse_dt(item.get("internal_date")),
    )


async def _upsert_message(
    session: AsyncSession,
    item: dict[str, Any],
    account_id: int,
    account_email: str,
    owner: str,
    result: SyncResult,
) -> bool:
    """Insert or update one message row; returns True if created. This is
    find-then-insert, but the sync loop is single-flight per source and the
    unique constraint is the backstop, so the TOCTOU window is closed."""
    gmail_id = str(item.get("gmail_id") or "")
    fields = parse_message_fields(item, account_email, owner)
    existing = (
        await session.execute(
            select(MailMessageRecord).where(
                MailMessageRecord.account_id == account_id,
                MailMessageRecord.gmail_id == gmail_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(MailMessageRecord(account_id=account_id, gmail_id=gmail_id, **fields))
        result.new += 1
        return True
    changed = False
    for key, value in fields.items():
        if key == "occurred_at":
            continue
        if getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
            if key in ("subject", "snippet"):
                setattr(existing, f"{key}_emb", None)
    if changed:
        existing.graph_ingested_at = None  # re-distill on material change
        result.updated += 1
    return False


# ---------------------------------------------------------------- sessions --
async def sync_agent_sessions(session: AsyncSession) -> tuple[SyncResult, dict[str, Any]]:
    cfg = get_settings()
    result = SyncResult()
    try:
        payload = await _get_json(cfg.agents_backend_url, "agents", "/api/agents/sessions")
    except Exception as exc:  # noqa: BLE001 — agents API is under active rebuild
        logger.warning("agent_sessions: source unavailable (%s)", exc)
        result.errors.append(str(exc))
        return result, {}
    items = as_list(payload, "sessions", "items")
    for item in items:
        sid = str(item.get("id") or "")
        if not sid:
            continue
        existing = (
            await session.execute(select(AgentSessionRecord).where(AgentSessionRecord.source_id == sid))
        ).scalar_one_or_none()
        fields = dict(
            owner=str(item.get("owner") or ""),
            title=str(item.get("title") or ""),
            model=str(item.get("model") or ""),
            occurred_at=parse_dt(item.get("created_at")),
            last_activity=parse_dt(item.get("updated_at")),
        )
        if existing is None:
            session.add(AgentSessionRecord(source_id=sid, **fields))
            result.new += 1
        else:
            changed = False
            if existing.title != fields["title"]:
                existing.title = fields["title"]
                existing.title_emb = None
                changed = True
            if fields["last_activity"] and existing.last_activity != fields["last_activity"]:
                existing.last_activity = fields["last_activity"]
                changed = True
            if changed:
                result.updated += 1
    await session.flush()
    return result, {}


async def fetch_session_messages(source_id: str, after_seq: int) -> list[dict[str, Any]]:
    """Fetch a session's transcript messages with seq > after_seq. Tolerates the
    endpoint not existing yet (agents rebuild in flight) by returning []."""
    cfg = get_settings()
    for path in (
        f"/api/agents/sessions/{source_id}/messages",
        f"/api/agents/sessions/{source_id}",
    ):
        try:
            payload = await _get_json(cfg.agents_backend_url, "agents", path)
        except Exception:  # noqa: BLE001
            continue
        items = as_list(payload, "messages", "items")
        out = []
        for m in items:
            seq = m.get("seq")
            if isinstance(seq, int) and seq > after_seq:
                out.append(m)
        if out or items:
            return sorted(out, key=lambda m: m.get("seq", 0))
    return []


def message_text(message: dict[str, Any]) -> str:
    """Extract human-readable text from a persisted pydantic-ai ModelMessage row."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            texts = []
            for part in parts:
                if isinstance(part, dict):
                    t = part.get("content")
                    if isinstance(t, str) and t.strip():
                        texts.append(t.strip())
                elif isinstance(part, str) and part.strip():
                    texts.append(part.strip())
            return "\n".join(texts)
    return ""
