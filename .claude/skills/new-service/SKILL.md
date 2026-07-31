---
name: new-service
description: Scaffold a NEW Funnel Manager backend service from ONE spec so every ~11 touchpoint category (source, compose, k3s, netpol, overlay, realm, grants.py, policy data.json, CI, operator skills, agent doc, Flux, gateway/nginx, canary) is emitted in lockstep and born clean. Use when adding a new backend (internal-only, browser-facing, or hybrid) — NOT for editing an existing one. Emits a diff you review + hand off, then verifies grants↔data.json↔realm, kustomize, compose, and opa.
---

# new-service

Adding a backend touches ~11 categories of file, spread across the repo, that
must stay in lockstep (`grants.py` ↔ `data.json` ↔ the realm are machine-proven
equal; manifests must render; compose must parse). Hand-wiring all of them is
where drift is born. This skill emits **every leg from one spec** so a new
service is born lockstep-clean, then runs the gates.

**A service is a UNION of ingress surfaces, not a binary.** The spec captures
that union: statefulness, a browser surface, an SPA, and a set of internal
callers are independent toggles.

## Spec

A small JSON (or YAML) file, or equivalent flags:

```json
{
  "name": "world",       // short name — dir = compose service = container/DNS = GHCR image = /api prefix
  "port": 8008,          // container/DNS port (keep unique; existing: 8000/8001/8003/8004/8005/8006)
  "stateful": true,      // owns a dedicated <name>-db (CNPG in k3s, postgres:16 in compose) + a data.json pairing
  "browser": true,       // reached from the ingress gateway at /api/<name>/* (adds routes/azp/nginx/gateway legs)
  "ui": false,           // serves an SPA at /<name>/ (a <name>ui container — that SPA is a SEPARATE scaffold)
  "callers": [           // INTERNAL callers that RFC-8693-exchange toward this service
    {"from": "jobs", "path_prefix": "/internal/jobs"}
  ],
  "deps": []             // services THIS one calls (adds exchange edges + role grants + netpol egress + svc-<dep> scope)
}
```

- **internal-only backend** = `browser:false` (published on loopback like `mcp`/`jobs`;
  routing_class `eastwest`; gets an armed-idle east-west canary VS).
- **browser-facing / hybrid** = `browser:true` (gateway HTTPRoute + nginx location;
  routing_class `gateway`; gets an armed-idle gateway canary route). A hybrid is just
  `browser:true` **with** `callers`.

`routing_class` follows the canary taxonomy (`canary.sh`): **browser ⇒ gateway,
internal-only ⇒ eastwest** — one canary artifact per service, matching how
`search`/`mail`/`agents` are `gateway` and `leads`/`mcp`/`jobs` are `eastwest`.

## Usage

```bash
# from the repo root; install the chassis editable so --check reads the patched grants.py:
python3 -m pip install -e ./libs/fm_runtime

# spec file (preferred):
python3 .claude/skills/new-service/gen.py --spec .claude/skills/new-service/examples/world.json

# or flags:
python3 .claude/skills/new-service/gen.py --name world --port 8008 --browser \
    --caller jobs:/internal/jobs

python3 .claude/skills/new-service/gen.py --name hello --port 8007 --stateful \
    --caller mcp

# options: --stateful --browser --ui --caller from[:path_prefix] (repeatable)
#          --dep <service> (repeatable) --no-verify --manifest-out out.json
```

The driver **finishes by running the gates** (unless `--no-verify`):
`fm_runtime.export --check` for **both** realms, `kubectl kustomize` on
`apps/overlays/prod` + `infrastructure/gateway` + `infrastructure/mesh-policies`,
`docker compose config -q` on both files (it `touch`es `.env` and sets a
`KEYCLOAK_REALM_FILE` placeholder like `ci.yml` does), and `opa test deploy/policy`.
`opa` is not installed by default — if absent it is **skipped and noted as
CI-verified** (`ci.yml` `policy` job runs it).

## What it emits (the touchpoint matrix)

**Always** — `<svc>/` source skeleton (`app/main.py` calling
`fm_runtime.install(app, "<svc>", …)`, `app/config.py`, `app/database.py` if
stateful, root-context `Dockerfile` + `Dockerfile.dev`, `requirements.txt`,
`.dockerignore`, `.env.example`); compose dev+prod service blocks (+ dedicated
`<svc>-db` postgres + volume if stateful; loopback publish if NOT browser); k3s
`deploy/apps/base/<svc>/{deployment,service,serviceaccount,kustomization}.yaml`
(+ `base/data/<svc>-db/` CNPG cluster+backup if stateful) + `netpol/<svc>.yaml`
(+ `<svc>-db.yaml`) + the netpol/data kustomization lines + overlay
`resources`+`images` entries; realm **client** + `svc-<svc>` optional scope +
`aud-<svc>` audience mapper + `<svc>-access` realm role (its `/api/<svc>` prefix
+ any dependency prefixes) added to the `admin` composite, in **both** the dev
realm and the prod-example realm; `grants.py`
`_DEFAULT_ROLE_GRANTS[<svc>-access]` + `SERVICES` + `SVC_EXCHANGE_SCOPES` edges;
`data.json` `roles`+`callers`+`azp_allow` (+`pairings` if stateful); CI
`build-images.yml` matrix row + `build-canary.yml` (option + preflight/ctx/pin
routing-class cases) + `ci.yml` backends import-test entry; the operator
service-lists (`deploy.sh` SERVICES / `check.sh` BACKENDS / `canary.sh`
svc_class+routing_class); `deploy/CONVENTIONS.md` rows; `.claude/agents/<svc>-agent.md`;
Flux `deploy/clusters/prod/apps.yaml` healthCheck.

**Per BROWSER surface** — `data.json` `routes["/api/<svc>/"]` +
`azp_allow[<svc>] += frontend` + anonymous `/api/<svc>/health`; the frontend
realm client gets an `aud-<svc>` custom-audience mapper (so a browser token
carries the `<svc>` audience); gateway `httproutes.yaml` app-prod rule; netpol
ingress from `istio-ingress`; nginx upstream+location in `frontend/nginx.dev.conf`;
an **armed-idle** gateway canary route `gateway/canary/<svc>-canary.yaml`
(Exact-matches the cookie-gate secret; NOT yet listed in the gateway
kustomization — the canary tooling toggles that on activation). If `ui`: a
`WEB_APPS` tile + `/<svc>/` gateway route (the `<svc>ui` SPA container itself is
a **separate scaffold**, mirror `agentsui`).

**Per INTERNAL caller** — `callers[<svc>] += {from, path_prefix?}`;
`azp_allow[<svc>] += <caller>`; `SVC_EXCHANGE_SCOPES += (from,<svc>)` and the
caller's realm client gets the `svc-<svc>` optional scope; netpol ingress from
that caller. An internal-only service also gets an **armed-idle** east-west VS
`mesh-policies/canary/<svc>-canary-eastwest.yaml` (PRESENCE-matches
`x-fm-canary`, regex `.+`, secret-free, `gateways: [mesh]`).

**Per DEPENDENCY (`deps`)** — the `<svc>-access` role also grants `/api/<dep>`;
the `<svc>` realm client gets `svc-<dep>` optional scope; `SVC_EXCHANGE_SCOPES +=
(<svc>,dep)`; `azp_allow[dep] += <svc>` + `callers[dep] += <svc>`; netpol egress
to `dep`.

## Templates

All new-file content is a string template in `gen.py` with `%%placeholder%%`
substitution (no `.format` — avoids `${...}`/`{}` collisions in YAML/JSON), plus
programmatic assembly for the parts that toggle on `stateful`/`browser`:

- source: `TPL_CONFIG`, `TPL_DATABASE`, `TPL_DOCKERFILE`, `TPL_DOCKERFILE_DEV`,
  `TPL_DOCKERIGNORE`, `TPL_ENV_EXAMPLE`, `TPL_REQS_STATEFUL/STATELESS`, and
  `build_main()` / `build_config()` (assembled).
- k3s: `TPL_DEPLOYMENT` (+ `build_k8s_env_extra()`), `TPL_SERVICE`,
  `TPL_SERVICEACCOUNT`, `TPL_KUSTOMIZATION`; data: `TPL_DB_CLUSTER`,
  `TPL_DB_BACKUP`, `TPL_DB_KUSTOMIZATION`; netpol: `build_netpol()`, `TPL_DB_NETPOL`.
- compose: `build_compose_service()` (dev/prod, db block, loopback vs expose).
- canary: `TPL_GW_CANARY` (gateway, secret from `canary-cookie-gate.yaml`),
  `TPL_EW_CANARY` (east-west, secret-free).
- agent doc: `TPL_AGENT_MD`.

Existing files are modified with precise anchored `edit`/`insert_after`/
`edit_re`/`edit_json` helpers. `data.json` uses **string insertion** (preserves
its compact inline style); the two realms use **JSON round-trip with
`ensure_ascii=False`** (append-only ⇒ a minimal, reviewable diff). Anchors are
chosen to be **cumulative-safe** (insert-after-open, regex-append) so two
services can be scaffolded back-to-back in one session.

`examples/hello.json` (internal-only stateful) and `examples/world.json` (hybrid
browser+internal) are the reference specs used by the self-test.

## Guardrails

- Refuses to overwrite an existing file (won't clobber a real service).
- Rejects a `name` outside `[a-z][a-z0-9]{1,15}`, or a `dep` whose port isn't in
  `PORT_MAP` (add it there for a new dependency target).
- **This is infra scaffolding, not the whole service.** It emits import-clean
  stubs; real routers, models, and business logic are the owning service agent's
  work. Fully *arming* a canary additionally needs a `<svc>-canary` base
  Deployment + overlay images entry (the `canary` program) — this skill lays
  down the route/VS scaffold only.
- Any policy/realm/audience-scope/network-exposure change **must** go to
  `security-reviewer`. The emitted diff is a review artifact, not a
  self-approval.
