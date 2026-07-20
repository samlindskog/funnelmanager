# Deploy

## Topology

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

The OpenClaw agent is **off in prod** (it sits behind the `agent` Compose
profile) so it doesn't run a second Telegram poller against dev's bot token.
To enable it, give prod its own `TELEGRAM_BOT_TOKEN` in `.env.prod` and run
`docker compose --profile agent -f docker-compose.prod.yml --env-file .env.prod up -d`.

Because prod has no source checkout, the OpenClaw container's bind-mount
(`${OPENCLAW_STATE_DIR:-./openclaw}`) must be seeded on the host with the
**versioned** parts of `openclaw/` before enabling the profile:
`openclaw.json.example`, `skills/`, and `extensions/` (the `funnelmanager-auth`
plugin lives here and is what links Telegram senders to Funnel Manager
profiles). Copy that subtree from the repo to the host's `OPENCLAW_STATE_DIR`;
the `openclaw-init` compose service copies the template to `openclaw.json` on
first start, and from then on the live config is runtime state (the gateway
writes per-user agents/bindings into it) — never overwrite it with the
template on an established deployment. Everything else there is runtime state
OpenClaw creates on first run. Without `extensions/`, agent tool calls have no
session token and every Funnel Manager tool fails authorization.

## CI/CD (GitHub Actions)

- **`.github/workflows/ci.yml`** — runs on every PR and push to main: frontend
  typecheck/build/lint + Compose validation. No deploy.
- **`.github/workflows/deploy-prod.yml`** — deploys prod. Never triggered by a
  plain push to main; only by:
  - pushing a tag `v*` (`git tag v1.2.0 && git push origin v1.2.0`), or
  - the **Run workflow** button (`workflow_dispatch`), which takes a ref.

  A build job (matrix over the five services) builds each image on a GitHub
  runner with buildx + GHA layer cache and pushes to GHCR using the workflow's
  `GITHUB_TOKEN`. The deploy job then SSHes to usfr2 with the forced-command
  deploy key and streams — over stdin — the GHCR username, the ephemeral
  `GITHUB_TOKEN` (so the box can pull private packages without a stored PAT),
  and `docker-compose.prod.yml` at the deployed ref. The hook writes the compose
  file, `docker login`s, pulls, `up -d`s, prunes, and logs out.

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
  swapfile absorbs single-user dev spikes. `docker compose stop openclaw` if the
  agent's 2G is not needed while developing.
