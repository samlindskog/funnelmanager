"""Shared plumbing for the source connectors.

Connectors poll their source service's read API (GET-only, exchange edge
``knowledge->{svc}``; the background loop runs as the knowledge service
account, which holds ``knowledge-access``) with a persisted watermark, upsert
into the records store, and leave embedding/graph work to the runner's later
passes. Source responses are parsed defensively — the agents API in particular
is under active rebuild, so shape drift must degrade to a logged skip, never a
crash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SyncState

logger = logging.getLogger("knowledge.sync")


@dataclass
class SyncResult:
    new: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Accept a bare list or a dict wrapping the list under one of ``keys``
    (or the first list value found) — tolerant of source-API shape drift."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        for value in payload.values():
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def json_list(value: Any) -> list[str]:
    """mail stores address/label lists as JSON strings; accept either form."""
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return [str(x) for x in json.loads(value)]
        except (ValueError, TypeError):
            return []
    return []


def split_address(formatted: str) -> tuple[str, str]:
    """'Name <a@b.c>' -> (name, email); bare address -> ('', address)."""
    s = (formatted or "").strip()
    if "<" in s and s.endswith(">"):
        name, _, rest = s.rpartition("<")
        return name.strip().strip('"'), rest[:-1].strip().lower()
    return "", s.lower()


async def get_watermark(session: AsyncSession, source: str) -> dict[str, Any]:
    row = await session.get(SyncState, source)
    if row is None:
        return {}
    try:
        return json.loads(row.watermark or "{}")
    except ValueError:
        return {}


async def set_state(
    session: AsyncSession,
    source: str,
    *,
    watermark: dict[str, Any] | None = None,
    error: str = "",
    new: int = 0,
    duration_ms: float = 0.0,
) -> None:
    from sqlalchemy import func

    row = await session.get(SyncState, source)
    if row is None:
        row = SyncState(source=source)
        session.add(row)
    if watermark is not None:
        row.watermark = json.dumps(watermark)
    row.last_error = error[:2000]
    row.last_new = new
    row.duration_ms = duration_ms
    row.last_sync_at = func.now()


async def scalar_all(session: AsyncSession, stmt) -> list[Any]:
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "SyncResult",
    "as_list",
    "get_watermark",
    "json_list",
    "logger",
    "parse_dt",
    "scalar_all",
    "select",
    "set_state",
    "split_address",
]
