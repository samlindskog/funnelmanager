#!/usr/bin/env python3
"""Assert the mesh's hand-kept anonymous allowlist covers the code-owned one.

`deploy/policy/data.json`'s ``funnelmanager.config.anonymous`` is the source of
truth for every route that tolerates no principal (exported from the
``@anonymous`` annotations). The Istio ``require-principal`` DENY policy
(``deploy/infrastructure/mesh-policies/authorization-policies.yaml``) carries a
**third hand-kept copy** of those paths as its ``notPaths`` — a request with no
validated principal is denied unless its path is exempted there.

This is the leg the ``fm_runtime.export --check`` gate does NOT cover (that
export is dump-only for the anonymous list). Here we make the divergence a
build failure: **every** prod-namespace anonymous prefix in ``data.json`` must
be covered by the mesh ``require-principal`` ``notPaths``. A new anonymous
endpoint added to the code/policy but forgotten in the mesh would otherwise ship
a route the gateway blackholes for token-less callers.

Scope: the ``require-principal`` DENY lives in the ``prod`` namespace, so it only
gates workloads deployed there. Anonymous endpoints belonging to workloads in
OTHER namespaces (e.g. ``alloy`` in ``monitoring``, gated by the grafana-host
allow, not this policy) are intentionally excluded — see
``NON_PROD_NS_SERVICES``.

Exit 0 in sync; exit 1 (with the missing prefixes) on drift. No third-party deps
(stdlib only) so it runs in the CI ``policy`` job without extra installs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA_JSON = REPO / "deploy/policy/data.json"
MESH_YAML = REPO / "deploy/infrastructure/mesh-policies/authorization-policies.yaml"

# Anonymous services whose workloads do NOT run in the `prod` namespace and so
# are not gated by the prod `require-principal` DENY (they carry their own mesh
# allow). Their data.json anonymous prefixes are excluded from this check.
NON_PROD_NS_SERVICES = frozenset({"alloy"})

# Anonymous PREFIXES that serve a SUBTREE (a static SPA shell + its asset tree,
# the MCP transport, or the Apollo webhook secret-in-path). For these, Istio
# needs BOTH a bare exact `notPath` (the top-level path — Istio's `/x/*` prefix
# match is literally `/x/` and does NOT match bare `/x`) AND a `/x/*` glob (the
# subtree); a single form is insufficient and the mesh comments say so ("the
# exact bare path needs its own entry"). Keyed on the PREFIX (not the service):
# e.g. the `mcp` anonymous entry bundles the subtree `/mcp` WITH exact probe
# paths (`/healthz`…). Every prefix NOT listed here (probes, the OAuth callback,
# the frontend `/` whose assets are enumerated separately) is an exact endpoint
# needing only the bare form. A new subtree-serving anonymous route adds its
# prefix here (the Phase-6 scaffold emits this for a browser/SPA service).
SUBTREE_PREFIXES = frozenset(
    {"/search", "/mail", "/agents", "/mcp", "/api/leads/webhooks/apollo"}
)


def _load_data_anonymous() -> list[tuple[str, str]]:
    """(service, prefix) for every anonymous prefix in data.json, minus the
    non-prod-namespace services."""
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    anon = data["funnelmanager"]["config"]["anonymous"]
    out: list[tuple[str, str]] = []
    for entry in anon:
        service = str(entry.get("service", ""))
        if service in NON_PROD_NS_SERVICES:
            continue
        for prefix in entry.get("prefixes", []) or []:
            out.append((service, str(prefix)))
    return out


def _extract_require_principal_notpaths() -> list[str]:
    """The `notPaths` string list of the prod `require-principal` DENY policy.

    Parsed without a YAML dependency: this file is stdlib-only so it runs in the
    `policy` CI job with no extra install. The manifest is a stable, machine-kept
    block, so a scoped regex over the `require-principal` document is sufficient
    and avoids a PyYAML dependency in a job that otherwise only needs OPA.
    """
    text = MESH_YAML.read_text(encoding="utf-8")
    # Isolate the require-principal document (between `---` separators).
    docs = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    doc = next((d for d in docs if "name: require-principal" in d), None)
    if doc is None:
        raise SystemExit("could not find the require-principal AuthorizationPolicy")
    # Take everything after the `notPaths:` key and collect `- "…"` list items
    # until the indentation/section ends.
    _, _, after = doc.partition("notPaths:")
    paths: list[str] = []
    for line in after.splitlines():
        m = re.match(r'\s*-\s*"([^"]+)"\s*$', line)
        if m:
            paths.append(m.group(1))
            continue
        # Stop at the first non-list, non-comment, non-blank line after the list
        # started (a new YAML key at a shallower indent).
        if paths and line.strip() and not line.lstrip().startswith("#"):
            break
    if not paths:
        raise SystemExit("parsed zero notPaths from require-principal — parser drift")
    return paths


def _has_bare(prefix: str, notpaths: list[str]) -> bool:
    """True iff the mesh has an EXACT bare `notPath` for `prefix`.

    A `.../*` glob does NOT satisfy this — Istio's `/x/*` is a literal `/x/`
    prefix that never matches the bare `/x`, so a glob-only mesh would DENY the
    top-level anonymous navigation OPA permits.
    """
    return prefix in notpaths


def _has_subtree_glob(prefix: str, notpaths: list[str]) -> bool:
    """True iff a mesh `.../*` glob exempts the subtree under `prefix`."""
    for entry in notpaths:
        if entry.endswith("/*"):
            base = entry[:-2]
            if base == prefix or prefix.startswith(base + "/"):
                return True
    return False


def main() -> int:
    anon = _load_data_anonymous()
    notpaths = _extract_require_principal_notpaths()

    missing: list[tuple[str, str, str]] = []  # (service, prefix, what's missing)
    for svc, p in anon:
        if not _has_bare(p, notpaths):
            missing.append((svc, p, f'bare exact notPath "{p}"'))
        # Subtree-serving prefixes ALSO need the `/*` glob (a bare-only mesh
        # would DENY the asset subtree OPA permits).
        if p in SUBTREE_PREFIXES and not _has_subtree_glob(p, notpaths):
            missing.append((svc, p, f'subtree glob notPath "{p}/*"'))

    if missing:
        print(
            "MESH ANONYMOUS DRIFT: data.json anonymous prefixes NOT fully covered "
            "by the prod require-principal notPaths:",
            file=sys.stderr,
        )
        for svc, p, what in missing:
            print(f"  - {p!r} (service {svc!r}) — missing {what}", file=sys.stderr)
        print(
            "Add each to the require-principal notPaths in "
            "deploy/infrastructure/mesh-policies/authorization-policies.yaml "
            "(subtree-serving services need BOTH the bare path AND a `/*` glob; "
            "or, if the workload is not in the prod namespace, add its service to "
            "NON_PROD_NS_SERVICES here).",
            file=sys.stderr,
        )
        return 1

    print(
        f"mesh in sync: all {len(anon)} prod-namespace anonymous prefixes are "
        f"covered by the require-principal notPaths ({len(notpaths)} entries; "
        f"subtree prefixes {sorted(SUBTREE_PREFIXES)} require bare + glob)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
