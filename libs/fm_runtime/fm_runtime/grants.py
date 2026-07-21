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
