"""Internal-only routes (compose network only — nginx never routes /internal).

The OpenClaw harness calls ``POST /internal/openclaw/session`` with the
channel + device id of the human it is acting for. If that channel identity is
linked to a profile, it gets a real session token to attach to MCP requests
(the MCP server forwards it to the search/leads backends, which enforce
authorization through the auth service + OPA). If not, the call records a
pending channel request that an admin can assign to a user from the hub UI.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app import sessions, store
from app.config import Settings, get_settings
from app.schemas import OpenClawSessionIn, OpenClawSessionOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

# Reuse one active session per channel identity instead of minting a fresh
# session on every harness call.
_TOKEN_CACHE_PREFIX = "openclaw_token:"


def _cache_key(channel: str, device_id: str) -> str:
    return f"{_TOKEN_CACHE_PREFIX}{store.channel_key(channel, device_id)}"


async def revoke_channel_session(
    channel: str, device_id: str, settings: Settings | None = None
) -> None:
    """Kill the cached OpenClaw session for a channel identity (used on unlink,
    so a detached sender cannot keep acting as the user until the TTL runs out)."""
    settings = settings or get_settings()
    redis = sessions.get_redis(settings)
    cache_key = _cache_key(channel, device_id)
    cached = await redis.get(cache_key)
    if cached:
        await sessions.delete_session(cached, settings)
        await redis.delete(cache_key)


@router.post("/openclaw/session", response_model=OpenClawSessionOut)
async def openclaw_session(
    body: OpenClawSessionIn,
    settings: Settings = Depends(get_settings),
) -> OpenClawSessionOut:
    channel = (body.channel or "").strip().lower()
    device_id = str(body.device_id or "").strip()
    if not channel or not device_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="channel and device_id are required",
        )

    link = await store.get_channel_link(channel, device_id)
    if not link:
        request = await store.upsert_channel_request(
            channel, device_id, body.display_name
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "pending": True,
                "message": (
                    "This channel is not linked to a profile yet. An admin must "
                    "assign the pending channel request to a user."
                ),
                "channel": request.get("channel"),
                "device_id": request.get("device_id"),
            },
        )

    username = str(link.get("username") or "")
    user = await store.get_user(username)
    if not user:
        # Linked user was deleted; surface as a fresh pending request.
        await store.unlink_channel(channel, device_id)
        await store.upsert_channel_request(channel, device_id, body.display_name)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"pending": True, "message": "Linked profile no longer exists."},
        )

    redis = sessions.get_redis(settings)
    cache_key = _cache_key(channel, device_id)
    cached = await redis.get(cache_key)
    if cached:
        session = await sessions.get_session(cached, settings)
        if session and session.get("username") == username:
            return OpenClawSessionOut(
                access_token=cached,
                username=username,
                role=str(user.get("role") or ""),
                expires_in=await sessions.session_ttl_remaining(cached, settings),
            )

    token = await sessions.create_session(username, settings)
    ttl = max(1, int(settings.session_ttl_seconds))
    await redis.set(cache_key, token, ex=ttl)
    logger.info(
        "Issued OpenClaw session for %s via %s",
        username,
        json.dumps({"channel": channel, "device_id": device_id}),
    )
    return OpenClawSessionOut(
        access_token=token,
        username=username,
        role=str(user.get("role") or ""),
        expires_in=ttl,
    )
