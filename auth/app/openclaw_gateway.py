"""Client for the OpenClaw gateway's funnelmanager plugin routes.

The funnelmanager-auth OpenClaw plugin registers two gateway-token routes:

- ``POST /api/funnelmanager/pairing/approve`` — approving a code adds the
  sender to OpenClaw's channel allow-from store — the same thing ``openclaw
  pairing approve`` does on the CLI — so the whole Telegram onboarding can be
  driven from the hub.
- ``POST /api/funnelmanager/agents/sync`` — full-syncs a user's OpenClaw agent
  (``user-<username>``) and per-sender peer bindings in openclaw.json, called
  whenever the sender→user mapping changes (assign / unlink / user delete).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import HTTPException, status

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

APPROVE_PATH = "/api/funnelmanager/pairing/approve"
AGENT_SYNC_PATH = "/api/funnelmanager/agents/sync"


async def _gateway_post(
    path: str,
    body: dict,
    *,
    success_field: str,
    failure_prefix: str,
    default_reason: str,
    settings: Settings | None = None,
) -> dict:
    """POST to a plugin route on the OpenClaw gateway. Raises HTTPException on
    failure: 503 when no gateway token is configured, 502 when the gateway is
    unreachable/errors, 409 when the route did not report ``success_field``."""
    settings = settings or get_settings()
    if not settings.openclaw_gateway_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENCLAW_GATEWAY_TOKEN is not configured on the auth service",
        )
    url = f"{settings.openclaw_gateway_url.rstrip('/')}{path}"
    try:
        # Hard wall-clock ceiling: httpx's timeout is per phase (connect, read,
        # write), so a stalled gateway could otherwise hold callers — some of
        # which sit inside the admin write lock — for several phases in a row.
        async with asyncio.timeout(20.0):
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {settings.openclaw_gateway_token}"},
                )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OpenClaw gateway unreachable: {exc}",
        ) from exc
    if response.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OpenClaw gateway rejected the gateway token",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OpenClaw gateway error ({response.status_code})",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or not payload.get(success_field):
        reason = (
            payload.get("reason")
            if isinstance(payload, dict) and payload.get("reason")
            else default_reason
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{failure_prefix}: {reason}",
        )
    return payload


async def approve_pairing(
    channel: str, code: str, settings: Settings | None = None
) -> dict:
    """Approve one OpenClaw DM pairing code. Raises HTTPException on failure."""
    return await _gateway_post(
        APPROVE_PATH,
        {"channel": channel, "code": code},
        success_field="approved",
        failure_prefix="OpenClaw did not approve the pairing",
        # Most common cause: the pairing code expired or was already consumed.
        default_reason=(
            "pairing code not found (expired?) — ask the sender to message the bot again"
        ),
        settings=settings,
    )


async def sync_agent(
    username: str,
    senders: list[dict],
    removed: list[dict] | None = None,
    settings: Settings | None = None,
) -> dict:
    """Full-sync a user's OpenClaw agent + DM peer bindings to ``senders``
    (``[{"channel": ..., "device_id": ...}, ...]``; empty list strips all of
    the user's bindings). Senders in ``removed`` additionally get their DM
    pairing revoked (offboarding). Raises HTTPException on failure."""
    body: dict = {"username": username, "senders": senders}
    if removed:
        body["removed"] = removed
    return await _gateway_post(
        AGENT_SYNC_PATH,
        body,
        success_field="synced",
        failure_prefix="OpenClaw did not sync the agent",
        default_reason="gateway rejected the sync request",
        settings=settings,
    )
