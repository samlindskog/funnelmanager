# Deploy

**Production is the k3s cluster defined in this directory** (Flux GitOps, Istio
ambient mesh, OPA, Keycloak, CNPG Postgres, observability). Docker Compose is the
**local / dev-box** path only — prod does not run compose. The old single-box
compose-prod deployment is **retired**.

## Layout

- `bootstrap/` — one-time cluster install runbook + script.
- `clusters/prod/` — Flux entrypoint (the reconciliation root Flux watches).
- `infrastructure/` — Istio, OPA, Keycloak, CNPG, gateway, mesh-policies,
  observability.
- `apps/` — Kustomize `base/` + the `prod` overlay (the overlay carries the
  currently-deployed image pins).
- `keycloak/` — the realm (prod-hardened export imported into the cluster
  Keycloak).
- `policy/` — the OPA bundle (`data.json` + Rego), CI-tested.
- `CONVENTIONS.md` — **binding** names, labels, scheduling, resources, and
  secret refs for every manifest.

## Topology

Prod runs on the k3s cluster. Node **roles** (not names) are the contract —
schedule to the `role=` label, never a host:

| Role | Runs |
|---|---|
| `cp` (control plane, host `usfr4`) | k3s control plane + datastore, istiod |
| `edge` | ingress gateway + Keycloak only (the only node with public 80/443) |
| `worker1` | apps, CNPG Postgres, observability |
| `worker2` | apps headroom (canary / second-pod capacity) |

Per-node budgets, taints, and the full role table live in `CONVENTIONS.md`.
Node public IPs and SSH aliases live in the ops environment, not in this repo.

## CI/CD — k3s GitOps

**A deploy is a git commit, not an SSH push** — auditable and revertable. Images
are built on GitHub runners and pushed to
`ghcr.io/samlindskog/funnelmanager/<name>` as immutable `sha-<sha>` tags (plus
`v*` on release tags); the chosen tag is *pinned into the prod overlay's
kustomization and committed to `main`*. Flux (watching `main`) reconciles the pin
and rolls it out. The cluster never builds and is never SSH-deployed.

- **`.github/workflows/ci.yml`** — every PR / push to main: SPA build/lint,
  backend import checks, `opa check`/`opa test`, `kustomize build` of every infra
  dir + the overlays, and compose-file validation. No deploy.
- **`.github/workflows/build-images.yml`** — reusable: builds the **eleven
  images** (buildx + GHA cache) → GHCR as `sha-<sha>` + a moving tag, then pins
  the chosen overlay's `newTag` and commits to `main` (`[skip ci]`).
- **`.github/workflows/release-prod.yml`** — prod release. Triggered only by a
  `v*` tag push or the **Run workflow** button (ref input); pins the **prod**
  overlay. Gate it behind the `production` environment's required reviewer.

The eleven images are the ten deployed services — `frontend`, `searchui`,
`mailui`, `agentsui`, `search`, `leads`, `mail`, `mcp`, `jobs`, `agents` — plus
`backup` (the `mongo-backup` CronJob runner).

## Releasing & rollback

Releases are driven by the **`deploy-funnelmanager` skill** (commit → tag → watch
CI build/pin → Flux rollout → verify → optional rollback + Cloudflare purge); the
**`prod-health` skill** reads deployment/CI status read-only. Full-stack canaries
(cookie-gated, telemetry-enabled) are driven by the **`canary` skill**.

- **Release:** push a `v*` tag, or dispatch `release-prod` with a ref.
- **Rollback:** dispatch `release-prod` with an older ref, or revert the pin
  commit on `main` — Flux re-reconciles either way.

## Local / dev: Docker Compose

Compose is the **local and dev-box** path only. `usfr3` is the shared
develop-here box (source checkout + hot-reload dev stack behind a host-nginx TLS
proxy); the same `docker-compose.dev.yml` runs on a laptop. Production is
exclusively the k3s cluster above (the legacy `docker-compose.prod.yml`
single-box path was removed 2026-08-10).

```bash
cp .env.example .env      # set APOLLO_API_KEY
docker compose -f docker-compose.dev.yml up --build
```

See the repo-root `README.md` and `CLAUDE.md` for the compose commands, the
`iss`/JWKS split-horizon rules, and the env-var table.
