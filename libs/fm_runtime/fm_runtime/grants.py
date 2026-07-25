"""Role-grant authorization — the same rule OPA's ``grant_ok_for`` applies in
the mesh, enforced in-process where there is no mesh (docker compose).

Grants are the legacy ``{service, methods, path_prefix}`` shape keyed by realm
role (the JWT's ``realm_access.roles``), sourced from, in order:

- ``FM_ROLE_GRANTS``      — inline JSON: ``{"role": [{...grant...}], ...}``
- ``FM_ROLE_GRANTS_FILE`` — path to that mapping, or to a full OPA data.json
                            (the ``funnelmanager.roles`` subtree is used)
- built-in default        — mirrors ``deploy/policy/data.json``; the OPA
                            bundle and this default must describe the same
                            roles, so change them together.

Path matching is exact or segment-boundary prefix (``/api/search`` does not
authorize ``/api/searches``) — identical to authz.rego's ``_prefix_match``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from fm_runtime.settings import get_runtime_settings

# Keep in sync with deploy/policy/data.json (funnelmanager.roles).
_DEFAULT_ROLE_GRANTS: dict[str, list[dict[str, Any]]] = {
    "admin": [{"service": "*", "methods": ["*"], "path_prefix": "/"}],
    "internal-service": [
        {"service": "leads", "methods": ["*"], "path_prefix": "/api/leads"}
    ],
}

# Every logical service in the mesh — the audiences a token may name and the
# services whose grants/scopes exist. Mirrors deploy/policy/data.json's routes/
# callers surface. `agents`/`jobs` (this program) join `search`/`leads`/`mail`/
# `mcp` here; UIs (`frontend`/`mailui`/`agentsui`) are not token audiences.
SERVICES: tuple[str, ...] = (
    "search",
    "leads",
    "mail",
    "mcp",
    "jobs",
    "agents",
)

# One-hop token-exchange allowlist: (from_client, to_audience). A client may
# exchange toward `to` ONLY if it holds the `svc-<to>` optional client scope in
# Keycloak — Keycloak itself refuses any pair not present here. This table is
# the built-in MIRROR of what deploy/policy/data.json's `azp_allow` and the
# realm's svc-<service> scope assignments encode; change them together.
#
# SECURITY-REVIEW: these are the delegation edges of the whole mesh. Existing:
# search->leads, mcp->leads, agents->mcp (was agent-example->mcp). NEW in this
# phase: mcp->search, mcp->jobs, mcp->mail, jobs->search, jobs->agents,
# search->mail. Each widens who may act toward a service — audit deliberately.
SVC_EXCHANGE_SCOPES: tuple[tuple[str, str], ...] = (
    # existing
    ("search", "leads"),
    ("mcp", "leads"),
    ("agents", "mcp"),
    # new — MCP fans out to every backend it exposes tools for
    ("mcp", "search"),
    ("mcp", "jobs"),
    ("mcp", "mail"),
    # new — jobs subscribes to producers' streams / proxies their control API
    ("jobs", "search"),
    ("jobs", "agents"),
    # new — search reads mail's contacted set for the exclude_contacted filter
    ("search", "mail"),
)

# Clients that may legitimately appear as an inbound `azp` on a service WITHOUT
# a service-to-service exchange edge: browser/ingress SPA clients that call a
# service directly with a user token (no RFC 8693 hop). They are never `from`
# nodes of the one-hop exchange allowlist, so the drift guard skips them when it
# reconciles azp_allow / realm scopes against SVC_EXCHANGE_SCOPES.
NON_EXCHANGE_AZP: frozenset[str] = frozenset({"frontend", "mailui", "agentsui"})


def _normalize(raw: object) -> dict[str, list[dict[str, Any]]]:
    if isinstance(raw, dict) and isinstance(raw.get("funnelmanager"), dict):
        raw = raw["funnelmanager"].get("roles", {})
    if not isinstance(raw, dict):
        raise ValueError("role grants must be a JSON object keyed by role")
    out: dict[str, list[dict[str, Any]]] = {}
    for role, value in raw.items():
        if isinstance(value, dict):
            value = value.get("grants", [])
        if not isinstance(value, list):
            raise ValueError(f"grants for role {role!r} must be a list")
        out[str(role)] = [grant for grant in value if isinstance(grant, dict)]
    return out


@lru_cache
def role_grants() -> dict[str, list[dict[str, Any]]]:
    """Role -> grants table. Parsed once; malformed config raises (the
    middleware calls this at startup so it fails fast, not per-request)."""
    settings = get_runtime_settings()
    if settings.role_grants_json:
        return _normalize(json.loads(settings.role_grants_json))
    if settings.role_grants_file:
        with open(settings.role_grants_file, encoding="utf-8") as fh:
            return _normalize(json.load(fh))
    return _DEFAULT_ROLE_GRANTS


def _prefix_match(prefix: str, path: str) -> bool:
    if prefix == "/" or path == prefix:
        return True
    return path.startswith(prefix.rstrip("/") + "/")


def policy_mirror() -> dict[str, Any]:
    """The built-in policy mirror, for `python -m fm_runtime.export` to dump and
    diff against deploy/policy/data.json. Surfaces the services, the one-hop
    exchange (svc-scope) allowlist, and the role-grant table this library
    enforces in compose — so code and mesh policy can be proven in sync."""
    return {
        "services": list(SERVICES),
        "svc_exchange_scopes": [
            {"from": src, "to": dst} for src, dst in SVC_EXCHANGE_SCOPES
        ],
        "role_grants": role_grants(),
    }


def _scope_audience(scope: str) -> str | None:
    """The target audience a `svc-<audience>` client scope grants exchange to,
    or None if the scope is not an exchange scope. Uses the configured template
    (default ``svc-{audience}``) so a customized naming scheme still parses."""
    template = get_runtime_settings().exchange_scope_template
    prefix, _, suffix = template.partition("{audience}")
    if not scope.startswith(prefix) or not scope.endswith(suffix):
        return None
    audience = scope[len(prefix): len(scope) - len(suffix) if suffix else None]
    return audience or None


def verify_policy(
    data: dict[str, Any], realm: dict[str, Any] | None = None
) -> list[str]:
    """Prove the code ⇔ mesh-policy (⇔ realm) lockstep the comments advertise.

    Returns human-readable drift / over-grant messages (empty == in sync). This
    is the real assertion behind "change them together" — the previous mirror
    only DUMPED the tables, so a reviewer could not actually detect azp_allow
    drift. Checks:

    - **Exchange edges (bijection, modulo browser clients):** every
      SVC_EXCHANGE_SCOPES edge ``src->dst`` must be encoded in
      ``data.json``'s ``azp_allow[dst]``, and every non-browser azp listed in
      ``azp_allow`` must be a known SVC_EXCHANGE_SCOPES edge.
    - **Role grants:** the built-in ``_DEFAULT_ROLE_GRANTS`` must equal
      ``data.json``'s ``funnelmanager.roles``.
    - **Realm least-privilege (when a Keycloak realm is supplied):** no client
      may hold a ``svc-<x>`` optional client scope that is not a
      SVC_EXCHANGE_SCOPES edge — this catches a *leftover exchange grant*
      (e.g. a superseded example client that still holds ``svc-mcp``) that
      Keycloak would otherwise still honor even though the one-hop allowlist no
      longer includes it.
    """
    errors: list[str] = []
    config = data.get("funnelmanager", {}).get("config", {}) if isinstance(data, dict) else {}
    azp_allow: dict[str, Any] = config.get("azp_allow", {}) or {}
    code_edges = set(SVC_EXCHANGE_SCOPES)

    for src, dst in SVC_EXCHANGE_SCOPES:
        if src not in (azp_allow.get(dst) or []):
            errors.append(
                f"exchange edge {src}->{dst} is in SVC_EXCHANGE_SCOPES but "
                f"azp_allow[{dst!r}] does not list {src!r}"
            )
    for dst, srcs in azp_allow.items():
        for src in srcs or []:
            if src in NON_EXCHANGE_AZP:
                continue
            if (src, dst) not in code_edges:
                errors.append(
                    f"azp_allow[{dst!r}] lists {src!r} but SVC_EXCHANGE_SCOPES "
                    f"has no {src}->{dst} edge"
                )

    data_roles = _normalize(data)
    if data_roles != _DEFAULT_ROLE_GRANTS:
        errors.append(
            "funnelmanager.roles in data.json differ from grants.py "
            f"_DEFAULT_ROLE_GRANTS (data={data_roles!r} code={_DEFAULT_ROLE_GRANTS!r})"
        )

    if realm is not None:
        for client in realm.get("clients", []) or []:
            cid = str(client.get("clientId") or "")
            for scope in client.get("optionalClientScopes", []) or []:
                dst = _scope_audience(str(scope))
                if dst is None:
                    continue  # not an exchange scope (basic/profile/roles/…)
                if (cid, dst) not in code_edges:
                    errors.append(
                        f"realm client {cid!r} holds optional scope {scope!r} "
                        f"({cid}->{dst}) that is not a SVC_EXCHANGE_SCOPES edge "
                        "— least-privilege over-grant; remove the scope or add "
                        "the edge (and its azp_allow entry)"
                    )
    return errors


def grants_allow(roles: tuple[str, ...], service: str, method: str, path: str) -> bool:
    """True if any grant of any held role covers (service, method, path)."""
    table = role_grants()
    for role in roles:
        for grant in table.get(role, ()):
            if grant.get("service") not in (service, "*"):
                continue
            methods = grant.get("methods") or []
            if "*" not in methods and method not in methods:
                continue
            if _prefix_match(str(grant.get("path_prefix") or ""), path):
                return True
    return False
