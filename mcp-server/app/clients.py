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
import json
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

    async def stream_search(
        self,
        path: str,
        json_body: dict[str, Any],
        *,
        max_seconds: float = 180.0,
    ) -> dict[str, Any]:
        """Start an NDJSON search stream and return once page 1 is available.

        The search backend runs ingest as a detached job, so disconnecting after
        ``first_page`` (or ``complete``, whichever comes first) does not stop it —
        results keep persisting server-side, same as when a browser navigates away.
        Stream ``error`` events surface as :class:`UpstreamError`.
        """
        token = await self._auth.token()
        timeout = httpx.Timeout(connect=30.0, read=max_seconds, write=30.0, pool=30.0)
        last_progress: dict[str, Any] = {}
        for attempt in (0, 1):
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    self._url(path),
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/x-ndjson",
                    },
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        token = await self._auth.token(force_refresh=True)
                        continue
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        try:
                            detail = json.loads(body).get("detail", body)
                        except Exception:
                            detail = body
                        raise UpstreamError(
                            f"Search backend POST {path} failed "
                            f"({response.status_code}): {detail}"
                        )
                    buffer = ""
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            event_type = event.get("type")
                            if event_type == "error":
                                raise UpstreamError(str(event.get("detail") or "Search failed"))
                            if event_type == "progress":
                                last_progress = event
                            elif event_type in {"first_page", "complete"}:
                                return {
                                    "event": event_type,
                                    "data": event,
                                    "last_progress": last_progress,
                                }
            break
        raise UpstreamError("Search stream ended without a first_page or complete event")


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
