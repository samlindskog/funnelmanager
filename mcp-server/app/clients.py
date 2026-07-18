"""HTTP client for the leads backend.

The leads backend authorizes every request against the central auth service +
OPA, so every call here carries a bearer token. The token is per tool call:
the OpenClaw harness fetches a session token for the profile linked to the
channel it is acting for and passes it with each MCP request; this server
forwards it upstream unchanged.

``AuthSession`` remains as an optional dev fallback (MCP_SHARED_LOGIN_FALLBACK)
that logs in with shared credentials when a tool call arrives with no token.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings


class UpstreamError(RuntimeError):
    """Raised when a backend call fails; message is surfaced to the MCP client."""


MISSING_TOKEN_MESSAGE = (
    "No session token was provided for this tool call. Pass session_token "
    "(the OpenClaw harness injects it, or fetch one with the "
    "funnelmanager_session_token tool). Tokens expire — fetch a fresh one if "
    "this keeps failing."
)

INVALID_TOKEN_MESSAGE = (
    "The session token was rejected (expired or revoked). Fetch a fresh one "
    "with funnelmanager_session_token and retry."
)

FORBIDDEN_MESSAGE = (
    "The profile behind this session token is not authorized for this action "
    "(OPA policy denied it). An admin can adjust the profile's role/grants in "
    "the Funnel Manager hub."
)


def _error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
        return payload.get("detail", payload)
    except Exception:
        return response.text


def _raise_for_auth(response: httpx.Response, context: str) -> None:
    if response.status_code == 401:
        raise UpstreamError(f"{context}: {INVALID_TOKEN_MESSAGE}")
    if response.status_code == 403:
        raise UpstreamError(f"{context}: {FORBIDDEN_MESSAGE}")


class AuthSession:
    """Shared-credential session cache (dev fallback only)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._token: str | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.mcp_shared_login_fallback)

    async def token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if self._token and not force_refresh:
                return self._token
            url = f"{self._settings.auth_backend_url.rstrip('/')}/api/auth/login"
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    data={
                        "username": self._settings.auth_username,
                        "password": self._settings.auth_password,
                    },
                )
            if response.status_code >= 400:
                raise UpstreamError(
                    f"Auth login failed ({response.status_code}): {_error_detail(response)}"
                )
            token = response.json().get("access_token")
            if not token:
                raise UpstreamError("Auth login returned no access_token")
            self._token = str(token)
            return self._token


class TokenResolver:
    """Explicit per-call token, else the shared-login fallback (when enabled)."""

    def __init__(self, auth: AuthSession):
        self._auth = auth

    async def resolve(self, explicit: str | None) -> tuple[str, bool]:
        """Returns (token, is_fallback)."""
        cleaned = (explicit or "").strip()
        if cleaned:
            return cleaned, False
        if self._auth.enabled:
            return await self._auth.token(), True
        raise UpstreamError(MISSING_TOKEN_MESSAGE)

    async def refresh_fallback(self) -> str:
        return await self._auth.token(force_refresh=True)


class LeadsBackendClient:
    """Calls the internal leads backend (bearer token, like every service now)."""

    def __init__(self, settings: Settings, tokens: TokenResolver):
        self._settings = settings
        self._tokens = tokens

    def _url(self, path: str) -> str:
        base = self._settings.leads_backend_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        bearer, is_fallback = await self._tokens.resolve(token)
        response: httpx.Response | None = None
        for attempt in (0, 1):
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {bearer}"},
                )
            if response.status_code == 401 and is_fallback and attempt == 0:
                bearer = await self._tokens.refresh_fallback()
                continue
            break
        assert response is not None
        _raise_for_auth(response, f"Leads backend {method} {path}")
        if response.status_code >= 400:
            raise UpstreamError(
                f"Leads backend {method} {path} failed "
                f"({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()
