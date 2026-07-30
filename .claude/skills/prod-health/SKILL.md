---
name: prod-health
description: Read-only health & CI/CD status of the funnelmanager PRODUCTION deployment (k3s on usfr4). Use when asked to check prod health, is prod healthy, deployment status, did the deploy land, prod status, check the cluster, is the site up, any errors in prod, or version/deploy drift. This NEVER writes, deploys, or mutates — for releases/rollbacks use deploy-funnelmanager instead.
---

# prod-health

Read-only status/health verdict for funnelmanager **production**. It is the
inspection complement to `deploy-funnelmanager` (which releases). This skill
**never** writes, patches, deploys, reconciles, scales, or touches the realm —
every remote command is a `get` / `logs` / `flux get` / `rollout status` / read
`curl`. If asked to *fix* something, hand off to `deploy-funnelmanager`.

The driver is `.claude/skills/prod-health/check.sh` (paths are repo-root
relative). Default:

```bash
.claude/skills/prod-health/check.sh          # = 'all' — one-screen verdict
.claude/skills/prod-health/check.sh smoke    # public-only, no ssh
.claude/skills/prod-health/check.sh pods     # readiness (prod + identity)
.claude/skills/prod-health/check.sh drift    # running sha vs pin vs origin/main
.claude/skills/prod-health/check.sh flux     # Flux apps-prod status
.claude/skills/prod-health/check.sh ci       # release-prod runs + pin-landed
.claude/skills/prod-health/check.sh logs     # bounded error tail + KC exchange
```

Each line is prefixed ✅ / ⚠️ / ❌. `all` runs every section in the order
smoke → pods → drift → flux → ci → logs.

## Ground truth about prod (verified 2026-07-29 — the README is STALE)

- **k3s**, control plane **usfr4** (ssh alias). NOT usfr2/compose (retired;
  usfr2 is now just a `k3s-agent`). Namespaces: `prod` (apps) + `identity`
  (Keycloak). Istio mesh + OPA ext_authz. Flux GitOps reconciles `main`.
  Public: **https://x9bc433.win** behind Cloudflare.
- **flux/kubectl on usfr4 need root's kubeconfig** — bare `flux`/`kubectl` fail
  with `dial tcp [::1]:8080: connection refused`. The driver prefixes every call
  with `sudo -n kubectl …` and `sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  flux …`.
- **Images:** `ghcr.io/samlindskog/funnelmanager/<svc>:sha-<sha>`. The live pin
  is `deploy/apps/overlays/prod/kustomization.yaml` (`images: … newTag: sha-…`),
  committed by CI as `ci(prod): pin images to sha-… [skip ci]`. Deploys trigger
  on `v*` tags via the `release-prod` workflow.
- **Workloads:** backends `search leads mail mcp jobs agents`, frontends
  `frontend mailui agentsui`, all `Deployment`s (1 replica each in prod).
  StatefulSets: `etcd milvus minio mongo`. Databases are **CNPG** clusters whose
  instance pods are `app-db-1` (search), `mail-db-1`, `jobs-db-1`, `agents-db-1`
  (labelled `cnpg.io/podRole=instance`); Keycloak's DB `kc-db-1` lives in
  `identity`. The container inside each app pod is named after the service
  (istio sidecar is `istio-proxy`), so log scans use `logs deploy/<svc> -c <svc>`.

## What each signal proves

1. **smoke** (no ssh) — `GET /` → **200** with `<title>Sign in</title>`; the
   per-service `/api/{search,mail,agents}/whoami` unauth → **403**.
2. **pods** — deploy `readyReplicas/replicas`, statefulset readiness, CNPG DB
   pods, Keycloak in `identity`, and any prod pod not Running/Completed.
3. **drift** — three-way: running image sha **vs** the overlay pin **vs**
   `origin/main`. Flags mid-roll (running≠pin) and **deploy lag** (code commits
   merged to main but not yet released — the `[skip ci]` pin commit is excluded
   from the count).
4. **flux** — `apps-prod` revision / READY / SUSPENDED. Healthy = `READY=True`,
   `SUSPENDED=False`, revision == `origin/main` HEAD short-sha.
5. **ci** — last 3 `release-prod` runs + whether the newest `v*` tag's pin is on
   `origin/main`.
6. **logs** — bounded (`--tail=300`) error/critical-level scan of each backend +
   a count of Keycloak `TOKEN_EXCHANGE_ERROR`.

## Healthy-looking signals that are NOT bugs (do not "fix")

- **Unauthenticated `/api/*/whoami` returns 403, not 401.** OPA default-deny
  answers *before* any auth challenge. **403 is the healthy signal** — a 200 here
  would mean auth is open/broken. The one place a green check depends on a 4xx.
- **HEAD of `origin/main` is a `ci(prod): pin … [skip ci]` commit.** That is the
  normal resting state after a release; the real build sha is its parent, and the
  driver reports "no deploy lag" when only that pin commit sits above the pin.
- **A handful of Keycloak `TOKEN_EXCHANGE_ERROR`** (`subject_token validation
  failure`) is normal: an expired subject token mid-detached-job triggers the
  documented downgrade to client-credentials and logs one. A *surge* from a
  single `clientId` is the real signal.
- **`apps-dev` shows SUSPENDED** in `flux get kustomizations` — by design (saves
  worker capacity). Only `apps-prod` matters here.
- **Transient `503` on `search:8000/internal/jobs/v1/stream`** in `jobs` logs
  right after a rollout — the jobs subscriber retries with backoff until search
  is Ready again. Only sustained 503s past a rollout window are a problem.
- **A `mongo-backup-*` CronJob pod in `OOMKilled`** with a sibling attempt
  `Completed` = the job retried and succeeded. A run with **no** Completed
  sibling is the real failure.

## Gotchas

- **usfr4 cannot reach the public Keycloak URL** (`curl https://kc.x9bc433.win`
  → 000; Cloudflare hairpin). For realm/client inspection use `kcadm` **inside**
  the pod; `kcadm get client/ID --fields attributes` returns `{}` (broken) — do a
  full GET instead. This skill does none of that (read-only, and realm inspection
  is out of scope) but future extensions should know.
- **Cloudflare API from usfr4 must use `curl -4`** (IPv6 → "access token from
  location"). Not needed by this skill.
- **`set -uo pipefail`, not `-e`** — one failed signal must never abort the
  report. Keep it that way.
- If usfr4 is unreachable, `pods`/`drift`/`flux`/`logs` degrade with a message;
  `smoke` still gives an external verdict.

## Relationship to deploy-funnelmanager

`deploy-funnelmanager status`/`smoke` overlap loosely, but this skill goes deeper
on health (per-deploy readiness, three-way drift, deploy lag, log scans, KC
exchange errors) and is guaranteed read-only. Use `deploy-funnelmanager` to
*act* (release/rollback/reconcile/purge); use `prod-health` to *look*.
