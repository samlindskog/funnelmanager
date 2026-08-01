---
name: canary
description: Operate the funnelmanager full-stack canary — build a telemetry-enabled canary of a service from a feature ref, activate it (cookie-gated on x9bc433.win), retire an idle canary, and list/inspect canary workloads. Use when asked to canary a service, deploy/activate/ship a canary, put a branch behind the fm_debug session + fm_route=canary selector cookies, retire/scale down a canary, or list canary status. NOT for a normal prod release (use deploy-funnelmanager) or a raw health check (use prod-health).
---

# canary

The **lifecycle engine** for the funnelmanager header-routed canary. A canary is
a separate `<svc>-canary` Deployment beside stable `<svc>`, built from an
arbitrary feature ref as `<svc>:canary-<sha>`, reachable **only** by a host-only
`fm_debug=<secret>` session cookie **plus** an `fm_route=canary` selector on
https://x9bc433.win (the `debug-session-gate` EnvoyFilter turns that pair into the
secret `x-fm-canary` header the app-prod HTTPRoute matches; `fm_debug` alone routes
to stable). Activation = the `build-canary.yml` workflow pins the tag and
flips replicas `0→1` on `main`; Flux reconciles. Retire = replicas back to `0`
(the ephemeral-canary pattern).

This skill only **acts** on canary lifecycle. It **delegates** everything a
sibling already owns — it does not re-implement flux/kubectl/curl/PromQL:

- flux reconcile + rollout-wait + cluster reads → **deploy-funnelmanager** (`deploy.sh reconcile`)
- post-activation drift + smoke verdict → **prod-health** (`check.sh drift` / `smoke`)
- traffic / idle queries → **observe-grafana** (Grafana MCP; agent-side)
- exercising a live canary in the browser → **drive-canary** (agent-side)

The driver is `.claude/skills/canary/canary.sh` (paths are repo-root relative).

## Routing classes (how the marker is steered) — ORTHOGONAL to SPA/backend

The driver classifies every canary two independent ways. `svc_class` (spa vs
backend) decides only whether the extra `--confirm-backend` gate applies.
`routing_class` decides **HOW** the `x-fm-canary` marker reaches the canary, and
therefore which toggled artifact + which kustomization the tooling operates on:

- **gateway** — edge-reachable services (`frontend searchui mailui agentsui search
  mail agents`): a cookie-gated **HTTPRoute** at
  `deploy/infrastructure/gateway/canary/<svc>-canary.yaml`, toggled in the
  `infra-gateway` kustomization. It **Exact-matches** the secret `x-fm-canary`
  header the EnvoyFilter injects, so the secret lives in each such route file (the
  byte-identical set in PLACEHOLDERS.md). `armed` fails closed if the match is
  missing/widened.
- **east-west** — internal-only services (`leads mcp jobs`, never through the
  ingress gateway): an in-mesh Istio **VirtualService** at
  `deploy/infrastructure/mesh-policies/canary/<svc>-canary-eastwest.yaml`, toggled
  in the `infra-mesh-policies` kustomization. It **presence-matches** `x-fm-canary`
  (regex `.+`, on `gateways: [mesh]`) and **must NOT contain the secret** — the
  marker is already non-forgeable in-mesh (the gateway strips client-supplied
  headers and re-injects the secret only for a valid cookie). `armed` fails closed
  if the secret leaks into a mesh VS or an ingress/`fm-gateway` entry appears in its
  `gateways:` list. An internal canary is reached by a marked request that
  **propagates** `x-fm-canary` across hops (e.g. a cookie'd canary `search` whose
  `search→leads` hop lands on `leads-canary`), never by an edge route.

## Prerequisites

- `gh` (authed) — dispatches + watches `build-canary.yml`.
- ssh alias `usfr4` — the k3s control plane (read the live replicas, wait the
  canary rollout).
- Sibling skills present: `deploy-funnelmanager`, `prod-health`. The idle/traffic
  query is run by the agent via **observe-grafana** (Grafana MCP).
- The service must already be **ARMED**: its `deploy/apps/base/<svc>-canary/`
  manifests, a `<svc>-canary` entry in
  `deploy/apps/overlays/prod/kustomization.yaml`, **and** its class-specific toggled
  route (a **gateway** HTTPRoute
  `deploy/infrastructure/gateway/canary/<svc>-canary.yaml`, or an **east-west**
  VirtualService `deploy/infrastructure/mesh-policies/canary/<svc>-canary-eastwest.yaml`
  — see *Routing classes* above). **Four services are armed today:** `frontend` +
  `searchui` (SPA, gateway), `search` (backend, gateway), and `leads` (backend,
  east-west/internal). This driver never scaffolds one — arming a canary is a
  reviewed trust decision.

## Verbs

```bash
.claude/skills/canary/canary.sh deploy <svc> <ref> [--confirm-backend]
.claude/skills/canary/canary.sh retire <svc> [--force]
.claude/skills/canary/canary.sh list        # = status
```

### `deploy <svc> <ref>` — build + activate a canary
1. **Requires armed.** If `<svc>` is not fully armed it FAILS with class-aware
   guidance pointing at the template to copy — `frontend-canary`/`searchui-canary`
   (SPA, gateway), `search-canary` (backend, gateway), or `leads-canary` (backend,
   east-west). It does **not** auto-scaffold.
2. **Drift-check:** diffs `<svc>-canary` against stable `<svc>` and warns on any
   divergence outside the known canary deltas (name, labels/selector, replicas,
   priorityClassName, affinity, image, `FM_DEPLOYMENT_VARIANT`). We deliberately
   keep the canary a **full copy** of stable (not patch-over-base), so this
   catches the env-copy drift trap.
3. **Backend gate:** for a backend canary it prints the two hard prerequisites and
   requires `--confirm-backend`:
   (a) the ref must be **schema-compatible** with stable — the canary runs stable's
   startup DDL against the **shared prod Postgres**, so a destructive migration
   breaks prod; and (b) build-canary **auto-activates** (replicas `0→1` from an
   arbitrary ref, no review) under stable's prod SA/identity — only deploy a
   **trusted** ref, and a GitHub `environment:` approval gate should protect
   activation. SPAs (`frontend`/`mailui`/`agentsui`) skip this gate.
4. `gh workflow run build-canary.yml -f service=<svc> -f ref=<ref>`, watches the
   run (it commits the pin+activate to `main`), then `git pull`.
5. **Delegates** the flux reconcile to `deploy-funnelmanager`, waits the
   `<svc>-canary` rollout (the sibling's fixed stable-service list omits
   canaries), then **delegates** verification to `prod-health` (`drift` confirms
   the canary runs its pinned `canary-<sha>`; `smoke` confirms the stable public
   surface is unharmed). Finally prints how to reach the canary via the
   `fm_debug` + `fm_route=canary` cookies (token lives in `PLACEHOLDERS.md` →
   *Debug session & canary access*).

### `retire <svc>` — scale an idle canary to 0
1. **Idle-check (fail-safe).** A shell driver can't call the Grafana MCP, so it
   cannot itself prove a canary is idle — and tearing down a workload you can't
   prove idle is unsafe. It prints the exact query to run via **observe-grafana**
   (`{service_name="<svc>"} | json | variant="canary"` over ~30m, or the
   `fm_http_*` series by variant) and **refuses without `--force`**. Run the
   observe-grafana query first; if idle, re-run with `--force`.
2. Scales down via a **GitOps commit** — seds `replicas: 1→0` in
   `deploy/apps/base/<svc>-canary/deployment.yaml` **and removes its class-specific
   route/VS line from the matching kustomization** (the gateway
   `canary/<svc>-canary.yaml` HTTPRoute from `infra-gateway`, or the east-west
   `canary/<svc>-canary-eastwest.yaml` VirtualService from `infra-mesh-policies`) —
   the canary-if-exists-else-stable toggle: no active canary ⇒ no route/VS ⇒ the
   `x-fm-canary` marker falls through to stable instead of 503'ing a zero-endpoint
   canary Service. Commits `[skip ci]`, pushes `main`, and delegates the reconcile
   to deploy-funnelmanager. `build-canary` re-adds the line on the next activation.
   The `canary-<sha>` pin stays in the overlay but is never pulled while idle.

### `list` / `status`
Shows every `<svc>-canary` workload: current replicas (ACTIVE vs idle), the
pinned `canary-*` tag from the overlay, and the observe-grafana query the agent
should run for last-seen canary traffic.

## Ground truth / gotchas

- **canary-if-exists-else-stable (route toggle).** A canary-marked request routes
  to `<svc>-canary` **only while that canary is active**; when idle it falls
  through to stable `<svc>`. This is enforced by tying ROUTE PRESENCE to
  activation: each `deploy/infrastructure/gateway/canary/<svc>-canary.yaml` is a
  separate HTTPRoute listed in the gateway kustomization **iff** `replicas > 0`.
  `build-canary` adds the line on activate; `retire` removes it. This is why an
  idle canary (`replicas: 0`) no longer 503s the `x-fm-canary` cookie. The gateway
  routes live in the `infra-gateway` Flux Kustomization (`prune: true`, so removing
  the line deletes the HTTPRoute); the Deployment lives in `apps-prod`, which
  `dependsOn: infra-gateway` — so on retire the route is pruned before the pods
  scale down (no 503 window), and on activate the route may briefly precede ready
  endpoints (transient, cookie holders only). **East-west canaries follow the
  identical toggle** with the VirtualService line in the `infra-mesh-policies`
  kustomization instead of the gateway one (e.g. `leads`), so an idle internal
  canary likewise has no VS and marked in-mesh traffic falls through to stable.
- **Armed today (4) — active vs idle.** `searchui-canary` (SPA, gateway),
  `search-canary` (backend, gateway — full prod-search identity, real prod Postgres
  + Apollo path via leads; treat activation as running unreleased code AS prod
  search), and `leads-canary` (backend, **east-west/internal** — no edge route;
  reached only in-mesh via a marked `search→leads` hop) are **ACTIVE** in git today
  (`replicas: 1`, route/VS listed in its kustomization). `frontend-canary` (SPA,
  gateway, egress-less — no `--confirm-backend`) is armed but **idle**
  (`replicas: 0`, no route line). Each ships its arming legs: base Deployment,
  overlay images entry, netpol, and its class-specific route/VS (a gateway
  HTTPRoute, or the east-west VirtualService for `leads`).
- **The cookies are the only way in.** Reaching a canary needs the host-only
  `fm_debug=<secret>` session cookie **plus** the `fm_route=canary` selector on
  `x9bc433.win`; the `debug-session-gate` EnvoyFilter strips any client-supplied
  `x-fm-canary` header and re-injects the secret only for that valid pair, so it
  isn't forgeable (`fm_debug` alone routes to stable). The secret is NOT in git —
  rotating it means updating the `fm-canary-token` Secret + `kubectl rollout
  restart deploy/istio-ingress` (see `PLACEHOLDERS.md` → *Debug session & canary
  access*); no manifest edit.
- **`backup` is not canary-able** — it's a batch job, not a Deployment.
- **`--force` on retire skips the idle proof** — only use it after confirming idle
  via observe-grafana, or when deliberately overriding.
- **flux/kubectl on usfr4 need root's kubeconfig** — the driver (like its
  siblings) prefixes remote calls with `sudo -n kubectl …`.
- **prod-health `drift` already understands canaries** — it compares each
  `*-canary`'s running tag to its own overlay `canary-*` pin (never to the stable
  `sha-*` pin), so it is the source of truth for "the canary is live and correct".

## See also

- **observe-grafana** — the Grafana MCP queries for canary traffic / idle checks
  (owned there; this skill delegates to it for `retire` and `list`).
- **drive-canary** — exercise a live canary end-to-end (set the cookie, walk the
  journey) once `deploy` has activated it.
- **deploy-funnelmanager** — the stable-release engine; this skill calls its
  `reconcile` and never re-implements flux/rollout.
- **prod-health** — the read-only health verdict; this skill calls its `drift` +
  `smoke` to verify an activation.
