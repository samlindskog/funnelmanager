"""The sync manager: interval loop + on-demand immediate sync.

Each cycle, per source, in order:
  1. records sync   — poll the source API with a watermark, upsert rows (cheap)
  2. embedding pass — embed NULL fuzzy-column embeddings (cheap: embedding API)
  3. graph pass     — distill un-ingested records into Graphiti episodes
                      (EXPENSIVE: LLM extraction). Cost-guarded two ways:
                      at most ``graph_auto_batch`` episodes per automatic cycle,
                      and if the backlog exceeds the P4 threshold the automatic
                      pass STOPS — a backlog that size only moves through an
                      explicitly confirmed knowledge_sync (estimate -> confirm).
  4. session pass   — distill new agent-session transcript turns into the
                      owner's per-user session graph (bounded per cycle).

All passes are single-flight (one asyncio lock); the MCP sync tool calls
``run_now`` which shares the same lock, so manual and automatic syncs never
interleave.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.database import Session
from app.embeddings import embed_texts, embeddings_available
from app.graph import graph_service, user_group_id
from app.models import (
    AgentSessionRecord,
    MailCampaignRecord,
    MailMessageRecord,
    SearchRecord,
    SyncState,
)
from app.sync import sources
from app.sync.base import SyncResult, set_state

logger = logging.getLogger("knowledge.sync")

_CONNECTORS = {
    "searches": sources.sync_searches,
    "mail_campaigns": sources.sync_mail_campaigns,
    "mail_messages": sources.sync_mail_messages,
    "agent_sessions": sources.sync_agent_sessions,
}

# (model, [(text_attr, emb_attr), ...]) per source — drives the embedding pass.
_EMBED_TARGETS = {
    "searches": (SearchRecord, [("query", "query_emb")]),
    "mail_campaigns": (MailCampaignRecord, [("name", "name_emb"), ("subject", "subject_emb")]),
    "mail_messages": (MailMessageRecord, [("subject", "subject_emb"), ("snippet", "snippet_emb")]),
    "agent_sessions": (AgentSessionRecord, [("title", "title_emb")]),
}

_GRAPH_MODELS = {
    "searches": SearchRecord,
    "mail_campaigns": MailCampaignRecord,
    "mail_messages": MailMessageRecord,
}


def _episode_for(source: str, rec: Any) -> tuple[str, str, str]:
    """(name, body, source_type) for a main-graph episode from a record."""
    if source == "searches":
        return (
            f"search-{rec.source_id}",
            f'{rec.owner or "someone"} ran a {rec.entity_type or "people"} search for '
            f'"{rec.query}" ({rec.total_results} results).',
            "text",
        )
    if source == "mail_campaigns":
        return (
            f"campaign-{rec.source_id}",
            json.dumps(
                {
                    "type": "email_campaign",
                    "owner": rec.owner,
                    "name": rec.name,
                    "subject": rec.subject,
                    "status": rec.status,
                    "send_strategy": rec.send_strategy,
                }
            ),
            "json",
        )
    if source == "mail_messages":
        who = rec.account_email or rec.owner or "a mailbox"
        if rec.direction == "outbound":
            head = f"{who} emailed {rec.counterparty_name or rec.counterparty_email}"
        else:
            head = f"{who} received email from {rec.counterparty_name or rec.counterparty_email}"
        return (
            f"mail-{rec.account_id}-{rec.gmail_id}",
            f"{head}. Subject: {rec.subject or '(none)'}. {rec.snippet or ''}".strip(),
            "text",
        )
    raise ValueError(source)


class SyncManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.last_cycle: dict[str, Any] = {}

    # ---------------------------------------------------------------- loop --
    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run_loop(), name="knowledge-sync-loop")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run_loop(self) -> None:
        cfg = get_settings()
        # Give the stack (db, keycloak, sources) a beat before the first pass.
        await asyncio.sleep(10)
        while not self._stopping.is_set():
            try:
                await self.run_now(list(_CONNECTORS), graph_batch=cfg.graph_auto_batch, auto=True)
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("sync cycle failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=cfg.sync_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------- entrypoint --
    async def run_now(
        self,
        source_names: list[str],
        *,
        graph_batch: int | None = None,
        auto: bool = False,
        records: bool = True,
    ) -> dict[str, Any]:
        """Run records+embedding+graph passes for the named sources. Serialized
        by the manager lock; ``auto`` applies the backlog cost-guard;
        ``records=False`` skips the source-poll + embedding passes (used by the
        gated /sync's second phase so it never re-polls the sources)."""
        cfg = get_settings()
        summary: dict[str, Any] = {}
        async with self._lock:
            for name in source_names if records else []:
                connector = _CONNECTORS.get(name)
                if connector is None:
                    summary[name] = {"error": f"unknown source {name!r}"}
                    continue
                started = time.monotonic()
                async with Session() as session:
                    try:
                        result, watermark = await connector(session)
                        await set_state(
                            session,
                            name,
                            watermark=watermark or None,
                            error="; ".join(result.errors),
                            new=result.new,
                            duration_ms=(time.monotonic() - started) * 1000,
                        )
                        await session.commit()
                        summary[name] = {"new": result.new, "updated": result.updated, "errors": result.errors}
                    except Exception as exc:  # noqa: BLE001
                        await session.rollback()
                        logger.exception("records sync failed for %s", name)
                        async with Session() as s2:
                            await set_state(s2, name, error=str(exc))
                            await s2.commit()
                        summary[name] = {"error": str(exc)}
            if records and embeddings_available():
                summary["embeddings"] = await self._embedding_pass(source_names)
            summary["graph"] = await self._graph_pass(source_names, graph_batch, auto=auto)
            if cfg.session_ingest_enabled and "agent_sessions" in source_names:
                summary["session_graphs"] = await self._session_pass()
        self.last_cycle = {"at": datetime.now(timezone.utc).isoformat(), "auto": auto, "summary": summary}
        return summary

    # ---------------------------------------------------------- embeddings --
    async def _embedding_pass(self, source_names: list[str], batch: int = 256) -> dict[str, int]:
        budget = get_settings().embed_backfill_per_cycle
        done: dict[str, int] = {}
        for name in source_names:
            target = _EMBED_TARGETS.get(name)
            if target is None:
                continue
            model, pairs = target
            count = 0
            async with Session() as session:
                for text_attr, emb_attr in pairs:
                    col_text = getattr(model, text_attr)
                    col_emb = getattr(model, emb_attr)
                    # Drain the backlog batch-by-batch up to the shared budget
                    # (one fixed batch per cycle made a 44k-row archive take
                    # ~170 cycles to embed).
                    while budget > 0:
                        rows = (
                            (await session.execute(
                                select(model).where(col_emb.is_(None), col_text != "")
                                .limit(min(batch, budget))
                            )).scalars().all()
                        )
                        if not rows:
                            break
                        vectors = await embed_texts([getattr(r, text_attr) or "" for r in rows])
                        embedded_any = False
                        for row, vec in zip(rows, vectors):
                            if vec is not None:
                                setattr(row, emb_attr, vec)
                                count += 1
                                embedded_any = True
                        budget -= len(rows)
                        await session.commit()
                        if not embedded_any:
                            # Every row in the batch embedded to None (blank
                            # text slipped the filter) — bail, don't spin.
                            break
                await session.commit()
            if count:
                done[name] = count
        return done

    # --------------------------------------------------------------- graph --
    async def graph_pending(self, source_names: list[str] | None = None) -> dict[str, int]:
        """Un-ingested record counts per graph-enabled source (the P4 estimate)."""
        cfg = get_settings()
        names = [n for n in (source_names or list(_GRAPH_MODELS)) if n in cfg.graph_source_set]
        pending: dict[str, int] = {}
        async with Session() as session:
            for name in names:
                model = _GRAPH_MODELS[name]
                n = (
                    await session.execute(
                        select(func.count()).select_from(model).where(model.graph_ingested_at.is_(None))
                    )
                ).scalar_one()
                if n:
                    pending[name] = int(n)
        return pending

    async def _graph_pass(
        self, source_names: list[str], batch: int | None, *, auto: bool
    ) -> dict[str, Any]:
        cfg = get_settings()
        names = [n for n in source_names if n in cfg.graph_source_set and n in _GRAPH_MODELS]
        if not names:
            return {"skipped": "no graph-enabled sources in this run"}
        if not graph_service.available:
            try:
                await graph_service.ensure()
            except RuntimeError as exc:
                return {"skipped": str(exc)}
        pending = await self.graph_pending(names)
        total_pending = sum(pending.values())
        if total_pending == 0:
            return {"ingested": 0}
        if auto and total_pending > cfg.sync_confirm_episodes:
            return {
                "skipped": (
                    f"backlog of {total_pending} episodes exceeds the auto threshold "
                    f"({int(cfg.sync_confirm_episodes)}); run knowledge_sync with confirmation (P4)"
                ),
                "pending": pending,
            }
        budget = batch if batch is not None else total_pending
        ingested = 0
        for name in names:
            if budget <= 0:
                break
            model = _GRAPH_MODELS[name]
            async with Session() as session:
                rows = (
                    (await session.execute(
                        select(model)
                        .where(model.graph_ingested_at.is_(None))
                        .order_by(model.occurred_at.asc().nullsfirst())
                        .limit(budget)
                    )).scalars().all()
                )
                for rec in rows:
                    ep_name, body, source_type = _episode_for(name, rec)
                    try:
                        await graph_service.add_episode(
                            group_id=cfg.main_group_id,
                            name=ep_name,
                            body=body,
                            source_description=f"funnelmanager {name} record (provenance {ep_name})",
                            reference_time=rec.occurred_at,
                            source=source_type,
                        )
                    except Exception as exc:  # noqa: BLE001 — one bad episode must not wedge the pass
                        logger.warning("graph ingest failed for %s %s: %s", name, ep_name, exc)
                        continue
                    rec.graph_ingested_at = datetime.now(timezone.utc)
                    # Commit PER EPISODE: add_episode is irreversible LLM spend,
                    # so a crash mid-batch must not roll back the stamps and
                    # re-ingest duplicates next cycle.
                    await session.commit()
                    ingested += 1
                    budget -= 1
        return {"ingested": ingested, "remaining": max(0, total_pending - ingested)}

    # ------------------------------------------------------ session graphs --
    async def _session_pass(self, max_messages: int = 200) -> dict[str, Any]:
        """Distill new agent-session transcript turns into per-user graphs."""
        if not graph_service.available:
            try:
                await graph_service.ensure()
            except RuntimeError as exc:
                return {"skipped": str(exc)}
        ingested = 0
        async with Session() as session:
            rows = (
                (await session.execute(
                    select(AgentSessionRecord)
                    .order_by(AgentSessionRecord.last_activity.desc().nullslast())
                    .limit(50)
                )).scalars().all()
            )
            for rec in rows:
                if ingested >= max_messages:
                    break
                messages = await sources.fetch_session_messages(rec.source_id, rec.graph_seq)
                if not messages:
                    continue
                lines: list[str] = []
                max_seq = rec.graph_seq
                for m in messages:
                    kind = str(m.get("kind") or "")
                    if kind not in ("user", "assistant"):
                        max_seq = max(max_seq, int(m.get("seq") or 0))
                        continue
                    text = sources.message_text(m)
                    if text:
                        speaker = rec.owner if kind == "user" else "assistant"
                        lines.append(f"{speaker}: {text[:1500]}")
                    max_seq = max(max_seq, int(m.get("seq") or 0))
                if lines:
                    try:
                        await graph_service.add_episode(
                            group_id=user_group_id(rec.owner),
                            name=f"agent-session-{rec.source_id}-seq{max_seq}",
                            body="\n".join(lines)[:12000],
                            source_description=(
                                f"agent session '{rec.title}' transcript (provenance agent-session:{rec.source_id})"
                            ),
                            reference_time=rec.last_activity,
                            source="message",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("session graph ingest failed for %s: %s", rec.source_id, exc)
                        continue
                    ingested += len(lines)
                rec.graph_seq = max_seq
                # Per-record commit — same irreversible-spend reasoning as the
                # graph pass: never re-ingest a transcript chunk after a crash.
                await session.commit()
        return {"messages_ingested": ingested}


sync_manager = SyncManager()
