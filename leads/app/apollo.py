import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException, status

from app.config import Settings

logger = logging.getLogger(__name__)

# Apollo enforces a per-minute cap per endpoint (e.g. 200/min for people search).
# Running several searches + enrichments concurrently trivially bursts past it, so
# transient 429s are retried with backoff instead of failing the whole stream.
_RATE_LIMIT_MAX_RETRIES = 4
_RATE_LIMIT_MAX_BACKOFF_SECONDS = 30.0


def apollo_query_params(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    """Flatten a JSON body into Apollo-style query parameters.

    Array values become repeated `field[]` params (Apollo rejects bare lists).
    Nested range objects like revenue_range: {min, max} become revenue_range[min].
    """
    params: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if value is None or value == "":
            continue
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if nested_value is None or nested_value == "":
                    continue
                params.append((f"{key}[{nested_key}]", nested_value))
            continue
        if isinstance(value, list):
            array_key = key if key.endswith("[]") else f"{key}[]"
            for item in value:
                if item is None or item == "":
                    continue
                params.append((array_key, item))
            continue
        params.append((key, value))
    return params


class ApolloLeadsClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        if not self.settings.apollo_api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="APOLLO_API_KEY is not configured on the leads server",
            )
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.settings.apollo_api_key,
        }

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        """Backoff for a 429: honor Retry-After if present, else exponential."""
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), _RATE_LIMIT_MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2.0**attempt, _RATE_LIMIT_MAX_BACKOFF_SECONDS)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.apollo_base_url.rstrip('/')}/{path.lstrip('/')}"
        query = apollo_query_params(params or {})
        headers = self._headers()
        async with httpx.AsyncClient(timeout=60.0) as client:
            for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=query,
                )
                if response.status_code != 429 or attempt >= _RATE_LIMIT_MAX_RETRIES:
                    break
                backoff = self._retry_after_seconds(response, attempt)
                logger.warning(
                    "Apollo rate limited (429) on %s; retrying in %.1fs (attempt %d/%d)",
                    path,
                    backoff,
                    attempt + 1,
                    _RATE_LIMIT_MAX_RETRIES,
                )
                await asyncio.sleep(backoff)

        if response.status_code == 429:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Apollo rate limit reached (too many requests in the last minute). "
                    "Wait a moment and run fewer searches/enrichments at once."
                ),
            )

        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"apollo_status": response.status_code, "apollo_error": detail},
            )

        data = response.json()
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Apollo returned a non-object payload",
            )
        return data

    async def search_people(self, params: dict[str, Any]) -> dict[str, Any]:
        """POST mixed_people/api_search — People API Search."""
        return await self._request("POST", "mixed_people/api_search", params=params)

    async def search_organizations(self, params: dict[str, Any]) -> dict[str, Any]:
        """POST mixed_companies/api_search — the free teaser variant (the paid
        /search variant consumes credits per returned record; people search
        already uses api_search for the same reason)."""
        return await self._request("POST", "mixed_companies/api_search", params=params)

    async def get_complete_person(
        self,
        apollo_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET people/{id} — Get Complete Person Info."""
        person_id = apollo_id.strip()
        if not person_id:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        return await self._request(
            "GET",
            f"people/{quote(person_id, safe='')}",
            params=params,
        )

    async def get_complete_organization(
        self,
        apollo_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET organizations/{id} — Get Complete Organization Info."""
        org_id = apollo_id.strip()
        if not org_id:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        return await self._request(
            "GET",
            f"organizations/{quote(org_id, safe='')}",
            params=params,
        )

    async def match_person(
        self,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST people/match — People Enrichment with full Apollo param passthrough."""
        return await self._request("POST", "people/match", params=params or {})

    async def proxy_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET an arbitrary Apollo relative path (under apollo_base_url) with query params."""
        cleaned = path.strip().lstrip("/")
        if not cleaned:
            raise HTTPException(status_code=400, detail="Apollo path is required")
        return await self._request("GET", cleaned, params=params or {})
