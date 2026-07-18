"""Redis-backed profile store: users, roles, channel links, pending requests.

Everything lives in a handful of Redis hashes so listing is one HGETALL:

- ``users``            field ``<username>``            -> user JSON
- ``roles``            field ``<name>``                -> role JSON
- ``channel_links``    field ``<channel>|<device_id>`` -> link JSON (has username)
- ``account_requests`` field ``<username>``            -> pending account request JSON
- ``channel_requests`` field ``<channel>|<device_id>`` -> pending channel request JSON

Sessions stay in ``sessions.py`` (per-token keys with native TTLs). Sessions
store only the username; role and existence are resolved from ``users`` on
every validation, so deleting a user or changing a role takes effect
immediately without sweeping sessions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.sessions import get_redis

USERS_KEY = "users"
ROLES_KEY = "roles"
CHANNEL_LINKS_KEY = "channel_links"
ACCOUNT_REQUESTS_KEY = "account_requests"
CHANNEL_REQUESTS_KEY = "channel_requests"

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
ROLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# Cap pending account requests so the anonymous request-account endpoint cannot
# grow Redis without bound (usernames are an effectively unlimited namespace).
MAX_ACCOUNT_REQUESTS = 500

ADMIN_ROLE = "admin"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def valid_username(value: str) -> bool:
    return bool(USERNAME_RE.fullmatch(value))


def valid_role_name(value: str) -> bool:
    return bool(ROLE_NAME_RE.fullmatch(value))


def channel_key(channel: str, device_id: str) -> str:
    return f"{channel.strip().lower()}|{str(device_id).strip()}"


def _loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def _hget(key: str, field: str) -> dict[str, Any] | None:
    return _loads(await get_redis().hget(key, field))


async def _hall(key: str) -> list[dict[str, Any]]:
    raw = await get_redis().hgetall(key)
    out = []
    for value in raw.values():
        data = _loads(value)
        if data:
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def get_user(username: str) -> dict[str, Any] | None:
    username = normalize_username(username)
    if not username:
        return None
    return await _hget(USERS_KEY, username)


async def list_users() -> list[dict[str, Any]]:
    users = await _hall(USERS_KEY)
    return sorted(users, key=lambda u: str(u.get("username") or ""))


async def create_user(username: str, password_hash: str, role: str) -> dict[str, Any]:
    user = {
        "username": normalize_username(username),
        "password_hash": password_hash,
        "role": role,
        "created_at": _now(),
    }
    await get_redis().hset(USERS_KEY, user["username"], json.dumps(user))
    return user


async def update_user(username: str, **fields: Any) -> dict[str, Any] | None:
    user = await get_user(username)
    if not user:
        return None
    user.update({k: v for k, v in fields.items() if v is not None})
    await get_redis().hset(USERS_KEY, user["username"], json.dumps(user))
    return user


async def delete_user(username: str) -> bool:
    username = normalize_username(username)
    removed = await get_redis().hdel(USERS_KEY, username)
    if removed:
        # Drop channel links pointing at the deleted user.
        for link in await list_channel_links():
            if link.get("username") == username:
                await unlink_channel(link.get("channel", ""), link.get("device_id", ""))
    return bool(removed)


async def count_users_with_role(role: str) -> int:
    return sum(1 for user in await list_users() if user.get("role") == role)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def normalize_grants(grants: Any) -> list[dict[str, Any]]:
    """Coerce role grants into [{service, methods (upper), path_prefix}]."""
    out: list[dict[str, Any]] = []
    if not isinstance(grants, list):
        return out
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        # Do NOT coerce a blank service or empty method list to "*": that would
        # silently turn a malformed grant into a wildcard (privilege escalation).
        # A blank service / empty methods simply match nothing.
        service = str(grant.get("service") or "").strip().lower()
        raw_methods = grant.get("methods")
        if isinstance(raw_methods, list):
            methods = [str(m).strip().upper() for m in raw_methods if str(m or "").strip()]
        else:
            methods = []
        path_prefix = str(grant.get("path_prefix") or "/").strip() or "/"
        if not path_prefix.startswith("/"):
            path_prefix = f"/{path_prefix}"
        out.append(
            {
                "service": service,
                "methods": methods,
                "path_prefix": path_prefix,
            }
        )
    return out


async def get_role(name: str) -> dict[str, Any] | None:
    name = (name or "").strip().lower()
    if not name:
        return None
    return await _hget(ROLES_KEY, name)


async def list_roles() -> list[dict[str, Any]]:
    roles = await _hall(ROLES_KEY)
    return sorted(roles, key=lambda r: str(r.get("name") or ""))


async def save_role(
    name: str, description: str, grants: list[dict[str, Any]]
) -> dict[str, Any]:
    role = {
        "name": name.strip().lower(),
        "description": description.strip(),
        "grants": normalize_grants(grants),
        "created_at": _now(),
    }
    await get_redis().hset(ROLES_KEY, role["name"], json.dumps(role))
    return role


async def delete_role(name: str) -> bool:
    return bool(await get_redis().hdel(ROLES_KEY, (name or "").strip().lower()))


async def roles_as_opa_data() -> dict[str, Any]:
    """Shape pushed to OPA: {<role>: {"grants": [...]}}."""
    return {
        str(role["name"]): {"grants": role.get("grants") or []}
        for role in await list_roles()
        if role.get("name")
    }


# ---------------------------------------------------------------------------
# Channel links (channel device id -> profile)
# ---------------------------------------------------------------------------


async def get_channel_link(channel: str, device_id: str) -> dict[str, Any] | None:
    return await _hget(CHANNEL_LINKS_KEY, channel_key(channel, device_id))


async def list_channel_links() -> list[dict[str, Any]]:
    return await _hall(CHANNEL_LINKS_KEY)


async def link_channel(
    channel: str, device_id: str, username: str, display_name: str = ""
) -> dict[str, Any]:
    link = {
        "channel": channel.strip().lower(),
        "device_id": str(device_id).strip(),
        "username": normalize_username(username),
        "display_name": display_name.strip(),
        "linked_at": _now(),
    }
    await get_redis().hset(
        CHANNEL_LINKS_KEY, channel_key(channel, device_id), json.dumps(link)
    )
    return link


async def unlink_channel(channel: str, device_id: str) -> bool:
    return bool(
        await get_redis().hdel(CHANNEL_LINKS_KEY, channel_key(channel, device_id))
    )


async def channels_for_user(username: str) -> list[dict[str, Any]]:
    username = normalize_username(username)
    return [link for link in await list_channel_links() if link.get("username") == username]


# ---------------------------------------------------------------------------
# Pending account requests (public "request an account" form)
# ---------------------------------------------------------------------------


async def upsert_account_request(username: str) -> dict[str, Any]:
    username = normalize_username(username)
    existing = await _hget(ACCOUNT_REQUESTS_KEY, username)
    # Bound the pending set: once full, refresh existing entries but do not add
    # new ones (the anonymous endpoint still answers 202 either way, so this
    # leaks nothing about which usernames are pending).
    if existing is None and await get_redis().hlen(ACCOUNT_REQUESTS_KEY) >= MAX_ACCOUNT_REQUESTS:
        return {"username": username, "requested_at": _now()}
    request = existing or {"username": username, "requested_at": _now()}
    request["last_seen_at"] = _now()
    await get_redis().hset(ACCOUNT_REQUESTS_KEY, username, json.dumps(request))
    return request


async def list_account_requests() -> list[dict[str, Any]]:
    requests = await _hall(ACCOUNT_REQUESTS_KEY)
    return sorted(requests, key=lambda r: str(r.get("requested_at") or ""))


async def delete_account_request(username: str) -> bool:
    return bool(
        await get_redis().hdel(ACCOUNT_REQUESTS_KEY, normalize_username(username))
    )


# ---------------------------------------------------------------------------
# Pending channel requests (unlinked channel device ids seen by OpenClaw)
# ---------------------------------------------------------------------------


async def get_channel_request(channel: str, device_id: str) -> dict[str, Any] | None:
    return await _hget(CHANNEL_REQUESTS_KEY, channel_key(channel, device_id))


async def upsert_channel_request(
    channel: str, device_id: str, display_name: str = ""
) -> dict[str, Any]:
    field = channel_key(channel, device_id)
    existing = await _hget(CHANNEL_REQUESTS_KEY, field)
    request = existing or {
        "channel": channel.strip().lower(),
        "device_id": str(device_id).strip(),
        "requested_at": _now(),
    }
    if display_name.strip():
        request["display_name"] = display_name.strip()
    request["last_seen_at"] = _now()
    await get_redis().hset(CHANNEL_REQUESTS_KEY, field, json.dumps(request))
    return request


async def list_channel_requests() -> list[dict[str, Any]]:
    requests = await _hall(CHANNEL_REQUESTS_KEY)
    return sorted(requests, key=lambda r: str(r.get("requested_at") or ""))


async def delete_channel_request(channel: str, device_id: str) -> bool:
    return bool(
        await get_redis().hdel(CHANNEL_REQUESTS_KEY, channel_key(channel, device_id))
    )
