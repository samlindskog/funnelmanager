"""Runtime settings, read once from FM_* env vars.

Kept dependency-free (no pydantic) so the library stays importable from any
service regardless of its own settings stack. Every service documents these
variables in its README section; they are identical across services except
FM_SERVICE_NAME / FM_OIDC_CLIENT_ID.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RuntimeSettings:
    # Logical service name; also the default OIDC client id and the audience
    # this service accepts on inbound tokens.
    service_name: str = field(default_factory=lambda: os.environ.get("FM_SERVICE_NAME", ""))

    # OIDC issuer (Keycloak realm), e.g. https://kc.example.com/realms/funnelmanager.
    # Token/JWKS URLs derive from it unless set explicitly.
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("FM_OIDC_ISSUER", ""))
    oidc_token_url: str = field(default_factory=lambda: os.environ.get("FM_OIDC_TOKEN_URL", ""))
    oidc_jwks_url: str = field(default_factory=lambda: os.environ.get("FM_OIDC_JWKS_URL", ""))
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("FM_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(
        default_factory=lambda: os.environ.get("FM_OIDC_CLIENT_SECRET", "")
    )

    # Local signature verification. In-mesh this is OFF: Istio
    # RequestAuthentication has already validated the JWT and the app only
    # parses it. Outside the mesh (docker-compose dev) turn it ON so tokens
    # are actually checked somewhere.
    jwt_verify: bool = field(default_factory=lambda: _bool("FM_JWT_VERIFY", False))

    # Reject tokens whose `aud` does not name this service (third layer —
    # Istio and OPA check it too). Audience defaults to the service name.
    enforce_audience: bool = field(default_factory=lambda: _bool("FM_ENFORCE_AUDIENCE", True))
    audience: str = field(default_factory=lambda: os.environ.get("FM_SERVICE_AUDIENCE", ""))

    # Client-scope name granting audience=<target> on exchanged tokens.
    # Keycloak requires the requester to hold an (optional) client scope with
    # an audience mapper for the target; ours are named svc-<client>.
    exchange_scope_template: str = field(
        default_factory=lambda: os.environ.get("FM_EXCHANGE_SCOPE_TEMPLATE", "svc-{audience}")
    )

    # Reject requests with no principal on routes not annotated @anonymous.
    # Defense-in-depth below the mesh DENY policy; keeps dev honest too.
    require_principal: bool = field(default_factory=lambda: _bool("FM_REQUIRE_PRINCIPAL", True))

    log_level: str = field(default_factory=lambda: os.environ.get("FM_LOG_LEVEL", "INFO"))

    @property
    def effective_audience(self) -> str:
        return self.audience or self.service_name

    @property
    def effective_client_id(self) -> str:
        return self.oidc_client_id or self.service_name

    @property
    def effective_token_url(self) -> str:
        if self.oidc_token_url:
            return self.oidc_token_url
        if self.oidc_issuer:
            return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"
        return ""

    @property
    def effective_jwks_url(self) -> str:
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        if self.oidc_issuer:
            return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"
        return ""

    @property
    def exchange_enabled(self) -> bool:
        """Token exchange runs whenever a token endpoint is configured;
        without one (bare dev) outbound calls pass the subject token through."""
        return bool(self.effective_token_url and self.oidc_client_secret)


@lru_cache
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()
