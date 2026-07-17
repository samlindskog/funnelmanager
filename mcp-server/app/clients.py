"""HTTP clients for the search and leads backends.

The MCP server is internal-only, but the search backend still validates every
bearer token against the auth backend — so ``AuthSession`` logs in with the
same shared credentials the UI uses, caches the opaque session token, and
refreshes it once when a request comes back 401 (session expiry / Redis flush).

The leads backend is internal-only and carries no request auth, matching how
the search backend's ``LeadsClient`` calls it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings


class UpstreamError(RuntimeError):
    """Raised when a backend call fails; message is surfaced to the MCP client."""


def _error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
        return payload.get("detail", payload)
    except Exception:
        return response.text


class AuthSession:
    """Session-token cache backed by the auth backend's OAuth2 password login."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._token: str | None = None
        self._lock = asyncio.Lock()

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


class SearchBackendClient:
    """Calls the search backend exactly like the browser does (bearer token)."""

    def __init__(self, settings: Settings, auth: AuthSession):
        self._settings = settings
        self._auth = auth

    def _url(self, path: str) -> str:
        base = self._settings.search_backend_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        token = await self._auth.token()
        response: httpx.Response | None = None
        for attempt in (0, 1):
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code == 401 and attempt == 0:
                token = await self._auth.token(force_refresh=True)
                continue
            break
        assert response is not None
        if response.status_code >= 400:
            raise UpstreamError(
                f"Search backend {method} {path} failed "
                f"({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()


class LeadsBackendClient:
    """Calls the internal leads backend (no request auth, like LeadsClient)."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def _url(self, path: str) -> str:
        base = self._settings.leads_backend_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"Leads backend {method} {path} failed "
                f"({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()
