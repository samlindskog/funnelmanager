"""Client for the OpenClaw gateway's funnelmanager pairing route.

The funnelmanager-auth OpenClaw plugin registers
``POST /api/funnelmanager/pairing/approve`` on the gateway (auth: gateway
token). Approving a code adds the sender to OpenClaw's channel allow-from
store — the same thing ``openclaw pairing approve`` does on the CLI — so the
whole Telegram onboarding can be driven from the hub.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, status

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

APPROVE_PATH = "/api/funnelmanager/pairing/approve"


async def approve_pairing(
    channel: str, code: str, settings: Settings | None = None
) -> dict:
    """Approve one OpenClaw DM pairing code. Raises HTTPException on failure."""
    settings = settings or get_settings()
    if not settings.openclaw_gateway_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENCLAW_GATEWAY_TOKEN is not configured on the auth service",
        )
    url = f"{settings.openclaw_gateway_url.rstrip('/')}{APPROVE_PATH}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                json={"channel": channel, "code": code},
                headers={"Authorization": f"Bearer {settings.openclaw_gateway_token}"},
            )
    except httpx.HTTPError as exc:
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
    if not isinstance(payload, dict) or not payload.get("approved"):
        # Most common cause: the pairing code expired or was already consumed.
        reason = (
            payload.get("reason")
            if isinstance(payload, dict) and payload.get("reason")
            else "pairing code not found (expired?) — ask the sender to message the bot again"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"OpenClaw did not approve the pairing: {reason}",
        )
    return payload
