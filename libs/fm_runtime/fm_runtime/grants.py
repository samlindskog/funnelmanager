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
#
# SECURITY-REVIEW: authorization model = one realm role per human-facing service
# API. Each `<service>-access` role grants FULL methods within exactly that
# service's `/api/<service>` prefix (principle 1: within a service every user
# sees the same data — the role only gates whether you may call the API at all,
# not which rows). `admin` keeps service:* (everything); `internal-service` stays
# leads-only (detached-job client-credentials identity).
#
# A role includes its service's DEPENDENCIES: authorization is the Keycloak role,
# and a role grants everything the service needs to do its job. `search` calls
# `leads` (Apollo/Mongo/Milvus for every result, similarity, hydration, enrich,
# credits), so `search-access` also grants `/api/leads` — otherwise a non-admin
# search user is denied at leads (the whole leads surface was admin-only before).
# This does NOT widen direct browser access: leads has no browser route (only the
# anonymous webhook path) and caller_ok fences it to the search/mcp workloads, so
# the grant is reachable only via the search->leads exchange hop. Change this
# table and deploy/policy/data.json together (verify_policy asserts they equal).
#
# SECURITY-REVIEW (internal-jobs trust boundary): `jobs-internal` is the ONLY
# role that may reach a producer's `/internal/jobs/*` endpoints (job-event stream
# reads + pause/resume/cancel control). It is a MACHINE role held by the `jobs`
# client's SERVICE ACCOUNT (client-credentials, azp=jobs) — NOT any human. BOTH
# the jobs stream subscriber AND the jobs control proxy authenticate as that
# service account; the acting human's identity rides only as AUDIT METADATA
# (e.g. an `X-FM-Acting-User` header), because the human was already authorized
# at the jobs MCP API. The grant is deliberately scoped to `/internal/jobs` on
# exactly the v1 job producers (`search`, `agents`) — no `/api/*` reach, no other
# services. It pairs with the jobs->search / jobs->agents exchange edges below.
_DEFAULT_ROLE_GRANTS: dict[str, list[dict[str, Any]]] = {
    "admin": [{"service": "*", "methods": ["*"], "path_prefix": "/"}],
    "internal-service": [
        {"service": "leads", "methods": ["*"], "path_prefix": "/api/leads"}
    ],
    "search-access": [
        {"service": "search", "methods": ["*"], "path_prefix": "/api/search"},
        {"service": "leads", "methods": ["*"], "path_prefix": "/api/leads"},
    ],
    "mail-access": [
        {"service": "mail", "methods": ["*"], "path_prefix": "/api/mail"}
    ],
    "jobs-access": [
        {"service": "jobs", "methods": ["*"], "path_prefix": "/api/jobs"}
    ],
    "agents-access": [
        {"service": "agents", "methods": ["*"], "path_prefix": "/api/agents"}
    ],
    "jobs-internal": [
        {"service": "search", "methods": ["*"], "path_prefix": "/internal/jobs"},
        {"service": "agents", "methods": ["*"], "path_prefix": "/internal/jobs"},
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

# The Keycloak client scope (carried by the `fm-origin-passthrough` script
# mapper) that propagates the inbound subject_token's `fm_origin` onto each
# exchanged token (P3). Every token-exchanging client must carry it as a
# DEFAULT scope, or the exchange it performs silently resets `fm_origin` to the
# default `user` downstream — agent attribution is lost.
FM_ORIGIN_SCOPE: str = "fm-origin"

# The sole exchanging client that must NOT rely on the passthrough scope: the
# `agents` client MINTS `fm_origin=agent` on the first hop via its own hardcoded
# claim mapper. It is deliberately the only `from`-node of SVC_EXCHANGE_SCOPES
# without `fm-origin` as a default scope; every OTHER from-node must have it.
ORIGIN_MINTING_CLIENTS: frozenset[str] = frozenset({"agents"})


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


def _audience_scope(audience: str) -> str:
    """The `svc-<audience>` client scope name a client must hold to exchange
    toward ``audience`` — the forward of :func:`_scope_audience`. Uses the
    configured template so a customized naming scheme is respected."""
    return get_runtime_settings().exchange_scope_template.replace("{audience}", audience)


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
    - **Realm exchange completeness (when a realm is supplied):** the inverse of
      the over-grant check — for **every** SVC_EXCHANGE_SCOPES edge ``src->dst``
      the realm's ``src`` client must actually hold the ``svc-<dst>`` optional
      client scope, else Keycloak REFUSES the one-hop exchange the code expects
      to work (a silent *under-grant* that only surfaces at runtime).
    - **Realm origin passthrough (when a realm is supplied):** every
      token-exchanging client (a ``from``-node of SVC_EXCHANGE_SCOPES) must carry
      the ``fm-origin`` client scope as a **default** scope so ``fm_origin``
      survives its exchange hop — EXCEPT ``agents`` (:data:`ORIGIN_MINTING_CLIENTS`),
      which mints ``fm_origin=agent`` via its own hardcoded mapper. A from-node
      missing the default scope silently resets origin to ``user`` downstream
      (agent attribution lost) — this catches a new exchanging client added
      without wiring the passthrough scope.
    - **Realm role definitions (when a realm is supplied):** the realm must
      define **every** role in ``_DEFAULT_ROLE_GRANTS`` (else a grant the code
      enforces names a role Keycloak can never issue — dead policy), and the
      ``admin`` role must be a Keycloak **composite of exactly the ``-access``
      roles** (so ``admin`` actually confers each service's access; drift here
      would silently under- or over-grant admins). This is the third leg of the
      code ⇔ data.json ⇔ realm lockstep — previously kept by hand.
    - **Realm provisioning groups (when a realm is supplied):** every
      ``groups[*].realmRoles`` entry must be a realm-defined role **and** a
      human-facing ``-access``/``admin`` role — a group may never map a MACHINE
      role (``internal-service``/``jobs-internal``). See
      :func:`_verify_realm_groups`.
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
        errors.extend(_verify_realm_roles(realm))
        realm_clients: dict[str, dict[str, Any]] = {
            str(client.get("clientId") or ""): client
            for client in realm.get("clients", []) or []
            if isinstance(client, dict)
        }

        # Over-grant: a client holding a svc-<x> scope with no matching edge.
        for cid, client in realm_clients.items():
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

        # Under-grant: an edge the code expects but the realm client lacks the
        # svc-<dst> optional scope for — Keycloak would REFUSE that exchange.
        for src, dst in SVC_EXCHANGE_SCOPES:
            want = _audience_scope(dst)
            client = realm_clients.get(src)
            if client is None:
                errors.append(
                    f"realm does not define token-exchanging client {src!r} that "
                    f"SVC_EXCHANGE_SCOPES uses as a from-node ({src}->{dst})"
                )
                continue
            if want not in (client.get("optionalClientScopes") or []):
                errors.append(
                    f"realm client {src!r} is MISSING required optional scope "
                    f"{want!r} for the {src}->{dst} exchange edge — Keycloak would "
                    "refuse this one-hop exchange (under-grant); add the scope to "
                    "the client's optionalClientScopes"
                )

        # Origin passthrough: every exchanging client (from-node) must carry the
        # fm-origin scope as a DEFAULT scope, except the origin-minting client(s).
        for cid in sorted({src for src, _ in SVC_EXCHANGE_SCOPES}):
            if cid in ORIGIN_MINTING_CLIENTS:
                continue
            client = realm_clients.get(cid)
            if client is None:
                continue  # already reported by the under-grant loop
            if FM_ORIGIN_SCOPE not in (client.get("defaultClientScopes") or []):
                errors.append(
                    f"realm client {cid!r} is a token-exchanging client (from-node "
                    f"of SVC_EXCHANGE_SCOPES) but does NOT carry {FM_ORIGIN_SCOPE!r} "
                    "as a DEFAULT client scope — fm_origin will silently reset to "
                    "'user' on its exchange hop (agent attribution lost). Add "
                    f"{FM_ORIGIN_SCOPE!r} to defaultClientScopes, or add the client "
                    "to ORIGIN_MINTING_CLIENTS if it mints fm_origin itself"
                )

        errors.extend(_verify_realm_groups(realm))
    return errors


def _verify_realm_groups(realm: dict[str, Any]) -> list[str]:
    """Assert the realm's provisioning ``groups`` map only *safe* roles.

    Groups are the human-provisioning unit (assign roles to a group, add users to
    the group — see the ``provision-user`` skill). Two invariants, both fail-closed:

    - **Definedness:** every ``realmRoles`` entry on a group must be a realm role
      the realm actually defines — else adding a user to the group grants nothing
      (or the import fails), a silent dead mapping.
    - **Human-facing only:** a group may map **only** the per-service ``-access``
      roles or ``admin`` (the human-assignable set). It must **never** map a
      MACHINE role (``internal-service``, ``jobs-internal``) — those are held by
      service accounts via client-credentials, and a human dropped into such a
      group would silently gain a service identity's grants (a privilege-escalation
      path that bypasses the one-access-role-per-service model).

    Returns drift messages (empty == in sync). A realm with no ``groups`` key is a
    no-op (backwards compatible). This is the groups leg of the realm lockstep —
    see :func:`verify_policy`."""
    errors: list[str] = []
    realm_role_names = {
        str(role.get("name"))
        for role in (realm.get("roles", {}) or {}).get("realm", []) or []
        if isinstance(role, dict)
    }
    # The only roles a group may confer: the per-service access roles + admin.
    # Everything else in _DEFAULT_ROLE_GRANTS (internal-service, jobs-internal) is
    # a machine role and must never be reachable via group membership.
    human_assignable = {
        role for role in _DEFAULT_ROLE_GRANTS if role.endswith("-access")
    } | {"admin"}

    def _walk(groups: Any) -> None:
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            label = str(group.get("path") or group.get("name") or "?")
            for role in group.get("realmRoles", []) or []:
                role = str(role)
                if role not in realm_role_names:
                    errors.append(
                        f"realm group {label!r} maps realm role {role!r} that the "
                        "realm does not define — adding a user to it grants nothing "
                        "(dead mapping); define the role or drop it from the group"
                    )
                elif role not in human_assignable:
                    errors.append(
                        f"realm group {label!r} maps role {role!r} which is NOT a "
                        "human-facing '-access'/'admin' role — groups must never "
                        "confer a MACHINE role (e.g. 'internal-service', "
                        "'jobs-internal'); a human added to this group would gain a "
                        "service identity's grants (privilege escalation)"
                    )
            _walk(group.get("subGroups"))

    _walk(realm.get("groups", []))
    return errors


def _verify_realm_roles(realm: dict[str, Any]) -> list[str]:
    """Assert the Keycloak realm defines every role the code grants against, and
    that ``admin`` is a composite of exactly the ``-access`` roles. Returns drift
    messages (empty == in sync). This is the realm leg of the code ⇔ data.json ⇔
    realm role lockstep — see :func:`verify_policy`."""
    errors: list[str] = []
    realm_roles: dict[str, dict[str, Any]] = {
        str(role.get("name")): role
        for role in (realm.get("roles", {}) or {}).get("realm", []) or []
        if isinstance(role, dict)
    }

    for role in _DEFAULT_ROLE_GRANTS:
        if role not in realm_roles:
            errors.append(
                f"realm does not define realm role {role!r} that grants.py "
                "_DEFAULT_ROLE_GRANTS enforces — the grant names a role "
                "Keycloak can never issue (add it to the realm)"
            )

    access_roles = {
        role for role in _DEFAULT_ROLE_GRANTS if role.endswith("-access")
    }
    admin = realm_roles.get("admin")
    if admin is not None:
        if not admin.get("composite"):
            errors.append(
                "realm role 'admin' must be a composite of the -access roles "
                "but its 'composite' flag is not set"
            )
        admin_composites = set(
            (admin.get("composites") or {}).get("realm", []) or []
        )
        missing = access_roles - admin_composites
        extra = admin_composites - access_roles
        if missing:
            errors.append(
                "realm role 'admin' composite is missing -access role(s) "
                f"{sorted(missing)!r} — admins would not confer that service's "
                "access"
            )
        if extra:
            errors.append(
                "realm role 'admin' composite includes non -access role(s) "
                f"{sorted(extra)!r} — it must be exactly the -access roles"
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
