"""RFC 8693 token exchange against Keycloak, with caching.

Rule: a service NEVER forwards the token it received. For every outbound
internal hop it exchanges the inbound subject token for a new token whose
`aud` names the target service (Keycloak appends the `act` claim, preserving
the delegation chain). Exchanged tokens are cached per (subject, audience) —
never exchange per request.

Two modes:
- exchange (normal): FM_OIDC_TOKEN_URL + client credentials configured.
- passthrough (bare dev, no Keycloak): the subject token is reused as-is and
  client-credential tokens are empty. Keeps `docker compose` bring-up usable
  before an IdP exists; never valid in the mesh, where the target's audience
  check would reject the un-exchanged token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

from fm_runtime.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
GRANT_CLIENT_CREDENTIALS = "client_credentials"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

_CACHE_MAX_ENTRIES = 2048
_EXPIRY_SLACK_SECONDS = 30.0


class ExchangeError(Exception):
    """Token endpoint unreachable or refused the exchange. Callers map this
    to a 502/503 — never silently fall back to forwarding the subject token."""


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # monotonic deadline

    @property
    def fresh(self) -> bool:
        return time.monotonic() < self.expires_at


class TokenBroker:
    """Process-wide broker; one instance per service (module singleton)."""

    def __init__(self, settings: RuntimeSettings | None = None) -> None:
        self.settings = settings or get_runtime_settings()
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def token_for(self, audience: str, subject_token: str | None) -> str:
        """Access token to call `audience` with.

        With a subject token: RFC 8693 exchange (delegation — `act` chain).
        Without one (background jobs, anonymous-webhook-triggered work): a
        client-credentials token for this service's own identity.
        """
        if not self.settings.exchange_enabled:
            # Bare dev only (no token endpoint at all) — RuntimeSettings
            # .validate() rejects a configured endpoint with a blank secret
            # at startup, so this can never mask a missing prod secret.
            return subject_token or ""

        # The svc-<audience> optional client scope carries the audience mapper;
        # Keycloak only honors the audience request when it is in scope.
        scope = self.settings.exchange_scope_template.format(audience=audience)
        if subject_token:
            key = "x:" + hashlib.sha256(subject_token.encode()).hexdigest() + ":" + audience
            form = {
                "grant_type": GRANT_TOKEN_EXCHANGE,
                "subject_token": subject_token,
                "subject_token_type": TOKEN_TYPE_ACCESS,
                "requested_token_type": TOKEN_TYPE_ACCESS,
                "audience": audience,
                "scope": scope,
            }
        else:
            key = "cc:" + audience
            form = {"grant_type": GRANT_CLIENT_CREDENTIALS, "scope": scope}

        cached = self._cache.get(key)
        if cached and cached.fresh:
            return cached.access_token

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)  # re-check after waiting
            if cached and cached.fresh:
                return cached.access_token
            token = await self._request_token(form)
            self._store(key, token)
            return token.access_token

    async def _request_token(self, form: dict[str, str]) -> _CachedToken:
        form = {
            **form,
            "client_id": self.settings.effective_client_id,
            "client_secret": self.settings.oidc_client_secret,
        }
        try:
            response = await self._http().post(self.settings.effective_token_url, data=form)
        except httpx.HTTPError as exc:
            raise ExchangeError(f"Token endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise ExchangeError(
                f"Token endpoint rejected {form['grant_type']} "
                f"({response.status_code}): {detail}"
            )
        try:
            payload = response.json()
            access_token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in") or 60.0)
        except (ValueError, KeyError) as exc:
            raise ExchangeError(f"Malformed token response: {exc}") from exc
        return _CachedToken(
            access_token=access_token,
            expires_at=time.monotonic() + max(1.0, expires_in - _EXPIRY_SLACK_SECONDS),
        )

    def _store(self, key: str, token: _CachedToken) -> None:
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            # Drop expired entries first; if none, drop the soonest-to-expire.
            expired = [k for k, v in self._cache.items() if not v.fresh]
            for k in expired:
                self._cache.pop(k, None)
                self._locks.pop(k, None)
            if len(self._cache) >= _CACHE_MAX_ENTRIES:
                victim = min(self._cache, key=lambda k: self._cache[k].expires_at)
                self._cache.pop(victim, None)
                self._locks.pop(victim, None)
        self._cache[key] = token


_broker: TokenBroker | None = None


def get_broker() -> TokenBroker:
    global _broker
    if _broker is None:
        _broker = TokenBroker()
    return _broker
