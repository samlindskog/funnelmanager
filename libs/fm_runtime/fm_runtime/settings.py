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

    # Client id of the runtime-AI-agents service. When this service performs an
    # exchange as that client (or an inbound token was already stamped by it),
    # TokenBroker mints/propagates fm_origin=agent so persisted records read
    # "alice (via agent)". Purely an attribution signal — never an access gate.
    agents_client_id: str = field(
        default_factory=lambda: os.environ.get("FM_AGENTS_CLIENT_ID", "agents")
    )

    # Reject requests with no principal on routes not annotated @anonymous.
    # Defense-in-depth below the mesh DENY policy; keeps dev honest too.
    require_principal: bool = field(default_factory=lambda: _bool("FM_REQUIRE_PRINCIPAL", True))

    # Per-request role-grant authorization (the same rule OPA's grant_ok_for
    # applies in the mesh). OFF in-mesh — OPA is the enforcement point there;
    # ON in docker compose, where nothing else authorizes beyond audience.
    enforce_grants: bool = field(default_factory=lambda: _bool("FM_ENFORCE_GRANTS", False))
    # Grant data: inline JSON mapping role -> grants, or a file holding that
    # mapping / a full OPA data.json. Unset = built-in default (see grants.py).
    role_grants_json: str = field(default_factory=lambda: os.environ.get("FM_ROLE_GRANTS", ""))
    role_grants_file: str = field(
        default_factory=lambda: os.environ.get("FM_ROLE_GRANTS_FILE", "")
    )

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
        without one (bare dev) outbound calls pass the subject token through.
        A token endpoint with a blank client secret is a misconfiguration —
        ``validate()`` rejects it at startup so the broker can never silently
        forward un-exchanged tokens in a Keycloak-backed deployment."""
        return bool(self.effective_token_url and self.oidc_client_secret)

    def validate(self) -> None:
        """Fail fast on contradictory configuration (PrincipalMiddleware calls
        this at startup so a bad deployment crashes with a clear message
        instead of degrading into passthrough/tokenless outbound calls)."""
        if self.effective_token_url and not self.oidc_client_secret:
            raise RuntimeError(
                "FM_OIDC_CLIENT_SECRET is empty but a token endpoint is "
                f"configured ({self.effective_token_url}). Set the secret, or "
                "unset FM_OIDC_ISSUER/FM_OIDC_TOKEN_URL for bare-dev passthrough."
            )


@lru_cache
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()
