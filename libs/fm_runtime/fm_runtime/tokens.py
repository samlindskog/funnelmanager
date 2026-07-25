"""RFC 8693 token exchange against Keycloak, with caching.

Rule: a service NEVER forwards the token it received. For every outbound
internal hop it exchanges the inbound subject token for a new token whose
`aud` names the target service (KC 26.2 keeps the subject and stamps the
exchanging client in `azp` — no nested `act` chain — while a Keycloak mapper
preserves the inbound `fm_origin` claim across the hop). Exchanged tokens are
cached per (subject, audience, origin) — never exchange per request.

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

from fm_runtime.principal import ORIGIN_AGENT, ORIGIN_USER, Principal
from fm_runtime.settings import RuntimeSettings, get_runtime_settings

logger = logging.getLogger(__name__)

GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
GRANT_CLIENT_CREDENTIALS = "client_credentials"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

_CACHE_MAX_ENTRIES = 2048
_EXPIRY_SLACK_SECONDS = 30.0


def resolve_origin(principal: Principal | None, settings: RuntimeSettings) -> str:
    """Decide the fm_origin the broker associates with the next exchange.

    This is now a CACHE-KEY dimension, not a wire value: Keycloak's mapper sets
    the real fm_origin claim on the issued token (see TokenBroker.token_for).
    Returns:

    - `agent` if the inbound principal already carries fm_origin=agent (PROPAGATE
      — once agent, always agent for the rest of the call chain),
    - `agent` if the client initiating this exchange is the agents service
      (MINT — either this very service, or the inbound token's azp/actor),
    - `user` otherwise (the default for every human-initiated request).

    Attribution/caching only; it never widens access (audience + role grants
    still gate), and it mirrors what Keycloak's mapper will stamp so the two
    stay consistent.
    """
    agents_client = settings.agents_client_id
    if principal is not None and principal.origin == ORIGIN_AGENT:
        return ORIGIN_AGENT
    if agents_client and settings.effective_client_id == agents_client:
        return ORIGIN_AGENT
    if principal is not None and agents_client and principal.actor == agents_client:
        return ORIGIN_AGENT
    return ORIGIN_USER


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

    async def token_for(
        self,
        audience: str,
        subject_token: str | None,
        *,
        origin: str | None = None,
    ) -> str:
        """Access token to call `audience` with.

        With a subject token: RFC 8693 exchange (KC 26.2 keeps the subject and
        stamps the exchanging client in `azp` — no nested `act`). Without one
        (background jobs, anonymous-webhook-triggered work): a client-credentials
        token for this service's own identity.

        `origin` (``user``/``agent``) is a CACHE-KEY dimension only. Propagation
        of fm_origin onto the issued token is KEYCLOAK-NATIVE: a hardcoded/script
        mapper preserves the inbound subject token's fm_origin claim across every
        hop (and mints it on the agents client) while IGNORING request form
        params — so the broker does NOT send fm_origin to the token endpoint. It
        only folds `origin` (sourced from the current principal via
        ``resolve_origin``) into the cache key so an agent-context and a
        user-context token for the same subject+audience never collide.
        Attribution only — it never changes what the token may access.
        """
        if not self.settings.exchange_enabled:
            # Bare dev only (no token endpoint at all) — RuntimeSettings
            # .validate() rejects a configured endpoint with a blank secret
            # at startup, so this can never mask a missing prod secret.
            return subject_token or ""

        # The svc-<audience> optional client scope carries the audience mapper;
        # Keycloak only honors the audience request when it is in scope.
        scope = self.settings.exchange_scope_template.format(audience=audience)
        origin_suffix = f":{origin}" if origin else ""
        if subject_token:
            key = (
                "x:"
                + hashlib.sha256(subject_token.encode()).hexdigest()
                + ":"
                + audience
                + origin_suffix
            )
            form = {
                "grant_type": GRANT_TOKEN_EXCHANGE,
                "subject_token": subject_token,
                "subject_token_type": TOKEN_TYPE_ACCESS,
                "requested_token_type": TOKEN_TYPE_ACCESS,
                "audience": audience,
                "scope": scope,
            }
        else:
            key = "cc:" + audience + origin_suffix
            form = {"grant_type": GRANT_CLIENT_CREDENTIALS, "scope": scope}
        # NOTE: fm_origin is deliberately NOT sent as a form param. Propagation
        # is KEYCLOAK-NATIVE — a hardcoded/script mapper on the exchange stamps
        # fm_origin on the issued token (minting it on the agents client,
        # preserving the inbound subject token's claim on every other hop) and
        # ignores request params. `origin` here is only a cache-key dimension.

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
