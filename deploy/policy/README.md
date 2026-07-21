# OPA policy bundle

The single authorization decision point for the mesh: every Envoy (sidecars
in `prod`/`dev`, plus the edge gateway) calls its node-local OPA via
ext_authz, and OPA evaluates `data.funnelmanager.envoy.result`. Default
deny.

- `funnelmanager/envoy.rego` — Envoy entrypoint (allow / 403 + reasons).
- `funnelmanager/authz.rego` — the rules: issuer + per-service audience,
  the anonymous allowlist, workload-caller allowlists, role grants,
  `azp` delegation constraints, and TCP dedicated-dependency isolation.
- `data.json` — ALL deployment-specific facts. Day-2 changes are edits
  here, not policy rewrites:
  - add a user/role → `roles` (assign the realm role in Keycloak; agents'
    service accounts get roles the same way),
  - allow a new internal caller (e.g. an agent runner for `/mcp`) →
    `config.callers`,
  - add a dedicated dependency → one line in `config.pairings`,
  - add an anonymous endpoint → `config.anonymous` (mirror the
    `@anonymous` code annotation; `python -m fm_runtime.export` prints the
    code-side list to diff against).
- `funnelmanager/authz_test.rego` — `opa test deploy/policy` (CI-enforced);
  covers the mandated cases: internal happy path (both identities), wrong
  audience rejected, webhook allowed without JWT, delegation-constrained
  agent, dedicated dependency rejecting every foreign caller including the
  gateway, cross-env issuer rejection, gateway host/route behavior.

Shipping: `deploy/infrastructure/opa/kustomization.yaml` generates the
`fm-policy` ConfigMap from these files (tests excluded) — a merged commit
rolls the bundle to every OPA instance via Flux. The hostname sentinels
(`replace-*.example.com`) must be substituted at deploy time
(PLACEHOLDERS.md).
