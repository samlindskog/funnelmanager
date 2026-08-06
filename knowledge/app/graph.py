"""Graphiti layer: one shared application graph + per-user session graphs.

Namespacing uses Graphiti's ``group_id`` primitive inside the single Neo4j
store (community edition is single-database; group_id is Graphiti's designed
tenancy boundary and every query is namespaced by it):

- ``main``            — the application knowledge graph (cross-user, P1).
- ``user-<username>`` — that user's session-context graph. Per-user separation
  here is a deliberate, user-approved carve-out from P1 uniform visibility:
  session context is the acting user's own memory, written and read only under
  their principal (analogous to the sanctioned destructive-ownership gates).

Degrades cleanly (leads-Milvus pattern): missing Neo4j or OPENAI_API_KEY makes
``available`` False with a reason; the service stays healthy and the records
path keeps working.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger("knowledge.graph")

_RETRY_SECONDS = 60.0


def user_group_id(username: str) -> str:
    """Collision-proof per-user group id: distinct usernames must NEVER share a
    session graph. Slug normalization alone collides ('sam.doe'/'sam_doe' →
    'sam-doe'), so a short digest of the exact username disambiguates."""
    raw = username or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "user"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{get_settings().user_group_prefix}{slug}-{digest}"


class GraphService:
    """Lazy, failure-tolerant wrapper around graphiti_core.Graphiti."""

    def __init__(self) -> None:
        self._graphiti: Any = None
        self._lock = asyncio.Lock()
        self._last_failure: float = 0.0
        self.unavailable_reason: str = "not initialized"

    @property
    def available(self) -> bool:
        return self._graphiti is not None

    async def ensure(self) -> Any:
        """Return a ready Graphiti instance or raise RuntimeError(reason)."""
        if self._graphiti is not None:
            return self._graphiti
        async with self._lock:
            if self._graphiti is not None:
                return self._graphiti
            if time.monotonic() - self._last_failure < _RETRY_SECONDS:
                raise RuntimeError(self.unavailable_reason)
            cfg = get_settings()
            if not cfg.openai_configured:
                self._note_failure("OPENAI_API_KEY is not configured (Graphiti extraction/embedding needs it)")
                raise RuntimeError(self.unavailable_reason)
            try:
                from graphiti_core import Graphiti
                from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig
                from graphiti_core.llm_client import LLMConfig, OpenAIClient

                llm = OpenAIClient(
                    config=LLMConfig(
                        api_key=cfg.openai_api_key,
                        model=cfg.knowledge_llm_model,
                        small_model=cfg.knowledge_small_llm_model,
                    )
                )
                embedder = OpenAIEmbedder(
                    config=OpenAIEmbedderConfig(
                        api_key=cfg.openai_api_key,
                        embedding_model=cfg.openai_embedding_model,
                        embedding_dim=cfg.openai_embedding_dimensions,
                    )
                )
                graphiti = Graphiti(
                    cfg.neo4j_uri,
                    cfg.neo4j_user,
                    cfg.neo4j_password,
                    llm_client=llm,
                    embedder=embedder,
                )
                # Bounded: the neo4j driver retries internally for ~30s+ on an
                # unreachable/misbehaving store — an unbounded await here would
                # hang startup and every request that triggers a lazy ensure.
                await asyncio.wait_for(graphiti.build_indices_and_constraints(), timeout=25)
            except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 — any driver/import/conn/timeout error means "degraded"
                self._note_failure(f"graph store unavailable: {exc}")
                raise RuntimeError(self.unavailable_reason) from exc
            self._graphiti = graphiti
            logger.info("graphiti ready (neo4j=%s)", cfg.neo4j_uri)
            return graphiti

    def _note_failure(self, reason: str) -> None:
        self.unavailable_reason = reason
        self._last_failure = time.monotonic()
        logger.warning("graphiti unavailable: %s", reason)

    # -- episodes -----------------------------------------------------------
    async def add_episode(
        self,
        *,
        group_id: str,
        name: str,
        body: str,
        source_description: str,
        reference_time: datetime | None = None,
        source: str = "text",
    ) -> None:
        graphiti = await self.ensure()
        from graphiti_core.nodes import EpisodeType

        await graphiti.add_episode(
            name=name[:200],
            episode_body=body,
            source=EpisodeType(source),
            source_description=source_description[:500],
            reference_time=reference_time or datetime.now(timezone.utc),
            group_id=group_id,
        )

    # -- retrieval ----------------------------------------------------------
    def _group_ids(self, scope: str, username: str) -> list[str]:
        cfg = get_settings()
        if scope == "app":
            return [cfg.main_group_id]
        if scope == "user":
            return [user_group_id(username)]
        if scope == "all":
            return [cfg.main_group_id, user_group_id(username)]
        raise ValueError(f"unknown scope {scope!r} (app | user | all)")

    async def search(self, *, query: str, scope: str, username: str, limit: int) -> list[dict[str, Any]]:
        graphiti = await self.ensure()
        results = await graphiti.search(
            query=query,
            group_ids=self._group_ids(scope, username),
            num_results=limit,
        )
        out: list[dict[str, Any]] = []
        for edge in results:
            item: dict[str, Any] = {"fact": getattr(edge, "fact", str(edge))}
            for attr in ("valid_at", "invalid_at", "created_at"):
                v = getattr(edge, attr, None)
                item[attr] = v.isoformat() if isinstance(v, datetime) else v
            gid = getattr(edge, "group_id", None)
            if gid:
                item["group"] = "app" if gid == get_settings().main_group_id else "user"
            episodes = getattr(edge, "episodes", None)
            if episodes:
                item["episode_uuids"] = list(episodes)[:5]
            out.append(item)
        return out

    async def recent_episodes(self, *, scope: str, username: str, limit: int) -> list[dict[str, Any]]:
        graphiti = await self.ensure()
        episodes = await graphiti.retrieve_episodes(
            reference_time=datetime.now(timezone.utc),
            last_n=limit,
            group_ids=self._group_ids(scope, username),
        )
        out: list[dict[str, Any]] = []
        for ep in episodes:
            item = {
                "name": getattr(ep, "name", ""),
                "content": (getattr(ep, "content", "") or "")[:2000],
                "source_description": getattr(ep, "source_description", ""),
            }
            for attr in ("valid_at", "created_at"):
                v = getattr(ep, attr, None)
                if isinstance(v, datetime):
                    item[attr] = v.isoformat()
            out.append(item)
        return out


graph_service = GraphService()
