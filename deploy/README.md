# Deploy

> **k3s restructure:** the target deployment is now the 3-node k3s cluster
> defined in this directory — `bootstrap/` (install runbook + script),
> `clusters/prod/` (Flux entrypoint), `infrastructure/` (Istio, OPA,
> Keycloak, CNPG, gateway, observability), `apps/` (Kustomize base +
> dev/prod overlays), `keycloak/` (realm), `policy/` (OPA bundle, pending
> the Rego stop-gate), with conventions in `CONVENTIONS.md`. Everything
> below this banner documents the **legacy Docker-Compose deployment**,
> which remains the running interim until the cluster is bootstrapped.

## Topology (legacy compose)

Two boxes, one role each (both Debian 13, identical hardening — `sam` + key-only
sshd, ufw 22/443/mosh, fail2ban, unattended-upgrades, swapfile):

| Box | IP | SSH | Role | Dir | Compose file | Public URL |
|---|---|---|---|---|---|---|
| usfr2 (8GB) | `192.155.85.254` | `usfr2` | **prod** | `/home/sam/funnelmanager-prod` | `docker-compose.prod.yml` | https://x9bc433.win |
| usfr3 (4GB) | `192.81.135.223` | `usfr3` | **dev**  | `/home/sam/funnelmanager`      | `docker-compose.dev.yml`  | https://dev.x9bc433.win |

Both boxes are fronted by their own host nginx (TLS terminated there with the
Cloudflare origin cert `*.x9bc433.win` at `/etc/ssl/x9bc433/`, Cloudflare proxy
in front) reverse-proxying to the dockerized stack on loopback
(`HOST_BIND=127.0.0.1:`): prod → `127.0.0.1:8080`, dev → `127.0.0.1:5173`. usfr3
is the develop-here box (source checkout + hot-reload dev stack; the dev Vite
server sits behind the TLS proxy via `VITE_ALLOWED_HOSTS`/`VITE_HMR_PROTOCOL=wss`).
Prod's volumes are namespaced by the `funnelmanager-prod` project (pinned via
`name:`). The old dev stack that used to coexist on usfr2 is stopped (its `*_dev`
volumes are retained but idle; `docker volume rm funnelmanager_*_dev` on usfr2 to
reclaim the disk). DNS: `dev.x9bc433.win` A record → usfr3 (`192.81.135.223`),
proxied.

**Cross-box Mongo sync + Milvus reconcile** live as untracked ops scripts in
`~/ops/` on both boxes (`mongo-union-sync.sh`, `embed-reconcile.sh`,
`reconcile_embeddings.py`) — see "Data sync" below.

**Prod never builds.** The prod dir is not a source checkout — it only needs
`docker-compose.prod.yml` and `.env.prod`. Images are built on GitHub runners
and pushed to `ghcr.io/samlindskog/funnelmanager/<service>`; the box just pulls.
The `prod` image tag is a moving tag advanced on every deploy; each build also
pushes an immutable `sha-<sha>` tag (and `v*` on release tags) for pinning and
rollback.

## CI/CD (GitHub Actions) — k3s GitOps

The deploy model is **GitOps**: images are built on runners and pushed to
GHCR, then the immutable `sha-<sha>` tag is *pinned into the overlay
kustomization and committed to `main`*. Flux (watching `main`) reconciles the
pin and rolls it out. A deploy is a git commit, not an SSH push — auditable
and revertable.

- **`.github/workflows/ci.yml`** — every PR / push to main: frontend + mailui
  build/lint, backend import checks, **`opa check`/`opa test`** (25+ policy
  tests), **`kustomize build`** of every infra dir + both overlays, and
  compose-file validation. No deploy.
- **`.github/workflows/build-images.yml`** — reusable: builds the six images
  (buildx + GHA cache) → GHCR as `sha-<sha>` + a moving `prod`/`dev` tag, then
  pins the chosen overlay's `newTag` and commits to `main` (`[skip ci]`).
- **`.github/workflows/release-prod.yml`** — prod release. Triggered only by a
  `v*` tag push or the **Run workflow** button (ref input); pins the **prod**
  overlay. Gate it behind the `production` environment's required reviewer.
- **`.github/workflows/deploy-dev.yml`** — dev preview in the same cluster.
  **Run workflow** with a ref; pins the **dev** overlay (served at
  `https://dev.x9bc433.win`, funnelmanager-dev realm, same Istio/OPA path as
  prod). `apps-dev` ships suspended for capacity — `flux resume kustomization
  apps-dev` to bring it up, `flux suspend` to reclaim the worker.

Rollback: dispatch `release-prod` with an older ref, or revert the pin commit
on `main` (Flux re-reconciles either way).

## Manual deploy / rollback (on the box)

The images already on GHCR can be redeployed without CI:

```bash
cd /home/sam/funnelmanager-prod
# Re-pull the current `prod` tag (e.g. after a config-only .env.prod change):
docker compose -f docker-compose.prod.yml --env-file .env.prod pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

# Pin a specific build (rollback):
IMAGE_TAG=sha-<full-sha> docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --pull always
```

Rollback via CI: **Run workflow** on `deploy-prod.yml` with the old ref — it
rebuilds (cached) and re-advances the `prod` tag to that ref.

### Required GitHub repo secrets

| Secret | Value |
|---|---|
| `PROD_HOST` | `192.155.85.254` |
| `PROD_USER` | `sam` |
| `PROD_SSH_KEY` | private half of the CI-only ed25519 deploy key |

The CI key's public half is installed in `~sam/.ssh/authorized_keys` on usfr2
with `command="/home/sam/funnelmanager-prod-deploy-hook.sh",no-pty,...` so that
key can *only* trigger a prod deploy — it gets no shell. The hook script is
versioned at `deploy/prod-deploy-hook.sh`; re-install it there after changing it.

Optionally add a required reviewer to the GitHub **production** environment
(Settings → Environments) to gate each deploy behind an approval.

## Data sync (cross-box)

Mongo is the source of truth; Milvus is per-box and never synced. The `~/ops/`
scripts (untracked, present on both boxes) reconcile them:

- **`mongo-union-sync.sh <peer> [pull|push|both]`** — union/upsert of the `leads`
  collection between this box's Mongo and a peer over SSH. Deduped on `apollo_id`,
  newest-wins by `updated_at`; each box keeps its own `_id` (Postgres search
  history references local `_id`s, so they must stay stable). `both` (default)
  leaves both sides holding the union. Uses `mongodump | mongorestore` into a
  staging collection, then a `$merge`.
- **`embed-reconcile.sh`** → **`reconcile_embeddings.py`** — embeds + indexes into
  *this* box's Milvus every lead it is missing a vector for: both docs embedded on
  the other box whose vector never synced, AND `embedding:false` docs never
  embedded anywhere. Marks each `embedding:true` afterward. Bounded by local Milvus
  membership, so it's idempotent and only spends OpenAI on true gaps. Contrast
  `leads/scripts/reembed.py`, which DROPS and rebuilds the whole collection.

`mongo-union-sync.sh` **auto-runs `embed-reconcile.sh`** on whichever box(es)
received data (local for `pull`, peer for `push`, both for `both`); set
`SKIP_EMBED=1` to sync Mongo only.

Cross-box SSH uses a dedicated `funnelmanager-ops-sync` key on both boxes' `sam`
(peer aliases `usfr2`/`usfr3` in each box's `~/.ssh/config`).

Typical flow (one command): `~/ops/mongo-union-sync.sh usfr2 both` on usfr3 —
merges Mongo both ways and embeds each box's gaps.

## Memory posture (post-incident, 2026-07-17)

usfr2 froze once under memory-pressure livelock (thrash without an OOM kill) when
it ran dev+prod together. Dev has since moved to usfr3, so prod owns the 8GB box.
Defenses now in place:

- 8GB swapfile (`/swapfile`, in fstab) on usfr2; 6GB on usfr3.
- Every container carries a `mem_limit` in both compose files, so a runaway
  container gets OOM-killed inside its cgroup (and restarted by its restart
  policy) instead of stalling the host.
- Prod (usfr2, sole stack) caps are sized up for the 8GB box: Milvus 3G,
  Mongo/leads 1.5G, MCP 1G, etc. Mongo runs `--wiredTigerCacheSizeGB 0.5`
  (default assumes ~50% of host RAM per mongod). Milvus reads its cgroup limit
  and scales its internal watermarks accordingly.
- Dev (usfr3, 4GB) keeps the tighter caps from `docker-compose.dev.yml`; the 6GB
  swapfile absorbs single-user dev spikes.
