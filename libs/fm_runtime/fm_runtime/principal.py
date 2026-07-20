"""Principal model + JWT parsing/verification.

Every service receives the originating principal as a JWT forwarded by the
Istio ingress/sidecar (`forwardOriginalToken: true`). In-mesh the signature
has already been validated by RequestAuthentication, so the default here is
parse-without-verify; outside the mesh (compose dev) FM_JWT_VERIFY=true turns
on full JWKS verification so tokens are still checked somewhere.

The workload identity of the *calling pod* (the second identity in the
architecture) travels as the mTLS SPIFFE principal; Envoy surfaces it in the
`x-forwarded-client-cert` header, which we parse into `Peer` for logging and
app-level assertions. Authorization decisions on it belong to OPA, not here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
import jwt as pyjwt

from fm_runtime.settings import RuntimeSettings

logger = logging.getLogger(__name__)

_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
_JWKS_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class Actor:
    """One link of an RFC 8693 `act` delegation chain."""

    sub: str = ""
    client_id: str = ""

    @classmethod
    def from_claim(cls, claim: dict[str, Any]) -> "Actor":
        return cls(
            sub=str(claim.get("sub") or ""),
            client_id=str(claim.get("client_id") or claim.get("azp") or ""),
        )


@dataclass(frozen=True)
class Principal:
    """The originating end-user or client identity carried by the JWT."""

    sub: str
    username: str
    issuer: str = ""
    client_id: str = ""  # azp — the OAuth client the token was issued to
    audiences: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    act_chain: tuple[Actor, ...] = ()  # outermost actor first
    expires_at: int = 0
    claims: dict[str, Any] = field(default_factory=dict)
    raw_token: str = ""  # subject_token for outbound RFC 8693 exchange

    @property
    def is_delegated(self) -> bool:
        return bool(self.act_chain)


@dataclass(frozen=True)
class Peer:
    """Calling-workload identity from Envoy's x-forwarded-client-cert."""

    spiffe_id: str = ""

    @property
    def service_account(self) -> str:
        # spiffe://cluster.local/ns/<namespace>/sa/<serviceaccount>
        parts = self.spiffe_id.split("/")
        return parts[-1] if len(parts) >= 2 and "sa" in parts else ""

    @property
    def namespace(self) -> str:
        parts = self.spiffe_id.split("/")
        try:
            return parts[parts.index("ns") + 1]
        except (ValueError, IndexError):
            return ""


class TokenError(Exception):
    """Raised when a presented token is unusable (malformed, bad signature,
    expired, or wrong audience). The middleware maps this to a 401."""


class _JwksCache:
    """kid -> verification key, refreshed from the JWKS endpoint on TTL expiry
    or on an unknown kid (covers key rotation)."""

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    async def key_for(self, kid: str, jwks_url: str) -> Any:
        if kid in self._keys and (time.monotonic() - self._fetched_at) < _JWKS_TTL_SECONDS:
            return self._keys[kid]
        await self._refresh(jwks_url)
        key = self._keys.get(kid)
        if key is None:
            raise TokenError(f"Unknown signing key id {kid!r}")
        return key

    async def _refresh(self, jwks_url: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(jwks_url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TokenError(f"JWKS fetch failed: {exc}") from exc
        keys: dict[str, Any] = {}
        for jwk in payload.get("keys", []):
            kid = str(jwk.get("kid") or "")
            if not kid:
                continue
            try:
                keys[kid] = pyjwt.PyJWK(jwk).key
            except pyjwt.exceptions.PyJWKError:  # unsupported key type — skip
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()


_jwks_cache = _JwksCache()


def _act_chain(claims: dict[str, Any]) -> tuple[Actor, ...]:
    chain: list[Actor] = []
    node = claims.get("act")
    while isinstance(node, dict):
        chain.append(Actor.from_claim(node))
        node = node.get("act")
    return tuple(chain)


def principal_from_claims(claims: dict[str, Any], raw_token: str = "") -> Principal:
    aud = claims.get("aud")
    audiences: tuple[str, ...]
    if isinstance(aud, str):
        audiences = (aud,)
    elif isinstance(aud, list):
        audiences = tuple(str(a) for a in aud)
    else:
        audiences = ()
    realm_access = claims.get("realm_access")
    roles = (
        tuple(str(r) for r in realm_access.get("roles", []))
        if isinstance(realm_access, dict)
        else ()
    )
    sub = str(claims.get("sub") or "")
    return Principal(
        sub=sub,
        username=str(claims.get("preferred_username") or sub),
        issuer=str(claims.get("iss") or ""),
        client_id=str(claims.get("azp") or ""),
        audiences=audiences,
        roles=roles,
        act_chain=_act_chain(claims),
        expires_at=int(claims.get("exp") or 0),
        claims=claims,
        raw_token=raw_token,
    )


async def parse_token(token: str, settings: RuntimeSettings) -> Principal:
    """Parse (and, when configured, verify) a bearer JWT into a Principal.

    Raises TokenError on anything unusable — the caller decides whether that
    is fatal (non-anonymous route) or ignorable (anonymous route).
    """
    if settings.jwt_verify:
        jwks_url = settings.effective_jwks_url
        if not jwks_url:
            raise TokenError("FM_JWT_VERIFY is on but no JWKS URL is configured")
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError as exc:
            raise TokenError(f"Malformed token: {exc}") from exc
        key = await _jwks_cache.key_for(str(header.get("kid") or ""), jwks_url)
        try:
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=_ALLOWED_ALGS,
                issuer=settings.oidc_issuer or None,
                # Audience is checked below with the shared logic so mesh and
                # dev behave identically.
                options={"verify_aud": False},
            )
        except pyjwt.InvalidTokenError as exc:
            raise TokenError(f"Token rejected: {exc}") from exc
    else:
        try:
            claims = pyjwt.decode(token, options={"verify_signature": False})
        except pyjwt.InvalidTokenError as exc:
            raise TokenError(f"Malformed token: {exc}") from exc
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            raise TokenError("Token expired")

    principal = principal_from_claims(claims, raw_token=token)
    if settings.enforce_audience and settings.effective_audience:
        if settings.effective_audience not in principal.audiences:
            raise TokenError(
                f"Token audience {list(principal.audiences)!r} does not include "
                f"{settings.effective_audience!r}"
            )
    return principal


def parse_peer(xfcc_header: str | None) -> Peer | None:
    """Extract the calling workload's SPIFFE URI from x-forwarded-client-cert.

    Envoy emits `Key=Value` pairs, semicolon-separated, per cert in the chain
    (comma-separated); the nearest hop is the first element.
    """
    if not xfcc_header:
        return None
    first = xfcc_header.split(",")[0]
    for pair in first.split(";"):
        key, _, value = pair.partition("=")
        if key.strip().lower() == "uri" and value:
            return Peer(spiffe_id=unquote(value.strip().strip('"')))
    return None


def summarize_for_log(principal: Principal | None) -> str:
    if principal is None:
        return "-"
    if principal.act_chain:
        actors = ">".join(a.client_id or a.sub for a in principal.act_chain)
        return f"{principal.username}(via {actors})"
    return principal.username


__all__ = [
    "Actor",
    "Peer",
    "Principal",
    "TokenError",
    "parse_peer",
    "parse_token",
    "principal_from_claims",
    "summarize_for_log",
]
