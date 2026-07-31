---
name: canary
description: Operate the funnelmanager full-stack canary — build a telemetry-enabled canary of a service from a feature ref, activate it (cookie-gated on x9bc433.win), retire an idle canary, and list/inspect canary workloads. Use when asked to canary a service, deploy/activate/ship a canary, put a branch behind the fm_canary cookie, retire/scale down a canary, or list canary status. NOT for a normal prod release (use deploy-funnelmanager) or a raw health check (use prod-health).
---

# canary

The **lifecycle engine** for the funnelmanager header-routed canary. A canary is
a separate `<svc>-canary` Deployment beside stable `<svc>`, built from an
arbitrary feature ref as `<svc>:canary-<sha>`, reachable **only** by a host-only
`fm_canary=<secret>` cookie on https://x9bc433.win (the `canary-cookie-gate`
EnvoyFilter turns the cookie into the secret `x-fm-canary` header the app-prod
HTTPRoute matches). Activation = the `build-canary.yml` workflow pins the tag and
flips replicas `0→1` on `main`; Flux reconciles. Retire = replicas back to `0`
(the ephemeral-canary pattern).

This skill only **acts** on canary lifecycle. It **delegates** everything a
sibling already owns — it does not re-implement flux/kubectl/curl/PromQL:

- flux reconcile + rollout-wait + cluster reads → **deploy-funnelmanager** (`deploy.sh reconcile`)
- post-activation drift + smoke verdict → **prod-health** (`check.sh drift` / `smoke`)
- traffic / idle queries → **observe-grafana** (Grafana MCP; agent-side)
- exercising a live canary in the browser → **drive-canary** (agent-side)

The driver is `.claude/skills/canary/canary.sh` (paths are repo-root relative).

## Prerequisites

- `gh` (authed) — dispatches + watches `build-canary.yml`.
- ssh alias `usfr4` — the k3s control plane (read the live replicas, wait the
  canary rollout).
- Sibling skills present: `deploy-funnelmanager`, `prod-health`. The idle/traffic
  query is run by the agent via **observe-grafana** (Grafana MCP).
- The service must already be **ARMED**: its `deploy/apps/base/<svc>-canary/`
  manifests **and** a `<svc>-canary` entry in
  `deploy/apps/overlays/prod/kustomization.yaml`. Only `frontend` (SPA) and
  `search` (backend) are armed today. This driver never scaffolds one — arming a
  canary is a reviewed trust decision.

## Verbs

```bash
.claude/skills/canary/canary.sh deploy <svc> <ref> [--confirm-backend]
.claude/skills/canary/canary.sh retire <svc> [--force]
.claude/skills/canary/canary.sh list        # = status
```

### `deploy <svc> <ref>` — build + activate a canary
1. **Requires armed.** If `<svc>` has no `<svc>-canary` base + overlay entry it
   FAILS with guidance pointing at the template to copy — `frontend-canary` (SPA)
   or `search-canary` (backend). It does **not** auto-scaffold.
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
   `fm_canary` cookie (token lives in `PLACEHOLDERS.md` → *Canary access*).

### `retire <svc>` — scale an idle canary to 0
1. **Idle-check (fail-safe).** A shell driver can't call the Grafana MCP, so it
   cannot itself prove a canary is idle — and tearing down a workload you can't
   prove idle is unsafe. It prints the exact query to run via **observe-grafana**
   (`{service_name="<svc>"} | json | variant="canary"` over ~30m, or the
   `fm_http_*` series by variant) and **refuses without `--force`**. Run the
   observe-grafana query first; if idle, re-run with `--force`.
2. Scales down via a **GitOps commit** — seds `replicas: 1→0` in
   `deploy/apps/base/<svc>-canary/deployment.yaml`, commits `[skip ci]`, pushes
   `main`, and delegates the reconcile to deploy-funnelmanager. The
   `canary-<sha>` pin stays in the overlay but is never pulled while idle.

### `list` / `status`
Shows every `<svc>-canary` workload: current replicas (ACTIVE vs idle), the
pinned `canary-*` tag from the overlay, and the observe-grafana query the agent
should run for last-seen canary traffic.

## Ground truth / gotchas

- **Armed today:** `frontend-canary` (SPA, static, egress-less — no `--confirm-backend`)
  and `search-canary` (the first backend canary — full prod-search identity, real
  prod Postgres + Apollo path via leads; treat activation as running unreleased
  code AS prod search).
- **The cookie is the only way in.** Reaching a canary needs the host-only
  `fm_canary=<secret>` cookie on `x9bc433.win`; the EnvoyFilter strips any
  client-supplied `x-fm-canary` header, so it isn't forgeable. Rotating the token
  means editing the EnvoyFilter Lua **and** the HTTPRoute match **and**
  `PLACEHOLDERS.md` (all three).
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
