"""Redis-backed store of active sessions (opaque bearer tokens).

Each login mints a random token mapped to a small JSON record. Redis holds it
under ``session:<token>`` with a native TTL, so expiry and the 1-day default are
enforced by the store itself; ``delete_session`` makes logout instant.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as redis

from app.config import Settings, get_settings

_SESSION_PREFIX = "session:"
_TOKEN_BYTES = 32

_client: redis.Redis | None = None


def get_redis(settings: Settings | None = None) -> redis.Redis:
    """Lazily build a shared async Redis client from settings."""
    global _client
    if _client is None:
        settings = settings or get_settings()
        _client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(token: str) -> str:
    return f"{_SESSION_PREFIX}{token}"


async def create_session(username: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    ttl = max(1, int(settings.session_ttl_seconds))
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    payload = {
        "username": username,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
    }
    await get_redis(settings).set(_key(token), json.dumps(payload), ex=ttl)
    return token


async def get_session(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    token = (token or "").strip()
    if not token:
        return None
    raw = await get_redis(settings).get(_key(token))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def delete_session(token: str, settings: Settings | None = None) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    removed = await get_redis(settings).delete(_key(token))
    return bool(removed)
