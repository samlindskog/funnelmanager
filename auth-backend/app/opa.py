"""OPA integration: the auth service owns the policy and the role data.

OPA runs as a bare ``opa run --server`` container with no mounted bundles, so
this module pushes both the Rego policy (``PUT /v1/policies/funnelmanager``)
and the role-grant data (``PUT /v1/data/funnelmanager/roles``) — at startup,
whenever roles change, and again automatically if a query comes back
undefined (which is what happens after an OPA restart wipes its state).

Decisions fail closed: if OPA is unreachable or the policy cannot be
evaluated, ``authorize`` raises and callers must deny the request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app import store
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

POLICY_ID = "funnelmanager"

# Role grants (pushed under data.funnelmanager.roles) are lists of
# {service, methods (uppercased, may be ["*"]), path_prefix}. A request is
# allowed when the caller's role has any grant matching all three dimensions.
POLICY_REGO = """\
package funnelmanager.authz

import rego.v1

default allow := false

allow if {
    some grant in data.funnelmanager.roles[input.role].grants
    service_matches(grant)
    method_matches(grant)
    path_matches(grant)
}

service_matches(grant) if grant.service == "*"

service_matches(grant) if grant.service == input.service

method_matches(grant) if "*" in grant.methods

method_matches(grant) if upper(input.method) in grant.methods

# "/" grants everything. Otherwise the request path must equal the prefix or sit
# below it at a segment boundary — so a grant for "/api/search/search" does not
# also authorize "/api/search/searches".
path_matches(grant) if grant.path_prefix == "/"

path_matches(grant) if input.path == grant.path_prefix

path_matches(grant) if startswith(input.path, boundary_prefix(grant.path_prefix))

boundary_prefix(prefix) := prefix if endswith(prefix, "/")

boundary_prefix(prefix) := concat("", [prefix, "/"]) if not endswith(prefix, "/")
"""


class OpaUnavailableError(RuntimeError):
    """OPA could not produce a decision; treat as deny (fail closed)."""


def _base_url(settings: Settings) -> str:
    return settings.opa_url.rstrip("/")


async def push_policy_and_data(settings: Settings | None = None) -> None:
    """Upload the Rego policy and current role grants to OPA."""
    settings = settings or get_settings()
    roles = await store.roles_as_opa_data()
    async with httpx.AsyncClient(timeout=10.0) as client:
        policy = await client.put(
            f"{_base_url(settings)}/v1/policies/{POLICY_ID}",
            content=POLICY_REGO.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        policy.raise_for_status()
        data = await client.put(
            f"{_base_url(settings)}/v1/data/{POLICY_ID}/roles",
            json=roles,
        )
        data.raise_for_status()
    logger.info("Pushed OPA policy and %d role(s)", len(roles))


async def push_roles_data(settings: Settings | None = None) -> None:
    """Upload only the role grants (used after role create/delete)."""
    settings = settings or get_settings()
    roles = await store.roles_as_opa_data()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.put(
            f"{_base_url(settings)}/v1/data/{POLICY_ID}/roles",
            json=roles,
        )
        response.raise_for_status()


async def _query_allow(
    input_doc: dict[str, Any], settings: Settings
) -> bool | None:
    """One OPA query; returns None when the decision is undefined."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{_base_url(settings)}/v1/data/{POLICY_ID}/authz/allow",
            json={"input": input_doc},
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "result" not in payload:
        return None
    return bool(payload["result"])


async def authorize(
    *,
    username: str,
    role: str,
    service: str,
    method: str,
    path: str,
    settings: Settings | None = None,
) -> bool:
    """Evaluate the policy for one request. Raises OpaUnavailableError on failure."""
    settings = settings or get_settings()
    input_doc = {
        "username": username,
        "role": role,
        "service": (service or "").strip().lower(),
        "method": (method or "").strip().upper(),
        "path": path or "/",
    }
    try:
        result = await _query_allow(input_doc, settings)
        if result is None:
            # Undefined decision — OPA probably restarted and lost the policy.
            await push_policy_and_data(settings)
            result = await _query_allow(input_doc, settings)
    except httpx.HTTPError as exc:
        raise OpaUnavailableError(f"OPA query failed: {exc}") from exc
    if result is None:
        raise OpaUnavailableError("OPA decision undefined after policy re-push")
    return result


async def reconcile_loop(interval_seconds: float = 60.0) -> None:
    """Background task: keep pushing policy/data so an OPA restart self-heals."""
    while True:
        try:
            await push_policy_and_data()
        except Exception:
            logger.warning("OPA policy push failed; retrying in %ss", interval_seconds)
        await asyncio.sleep(interval_seconds)
