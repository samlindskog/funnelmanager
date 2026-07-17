# Deploy

## Topology

usfr2 (`192.155.85.254`) runs **two independent Compose stacks** side by side,
each fronted by the host nginx (TLS terminated there, Cloudflare in front):

| Stack | Dir | Compose project | Compose file | Public URL | Frontend on loopback |
|---|---|---|---|---|---|
| dev  | `/home/sam/funnelmanager`      | `funnelmanager`      | `docker-compose.dev.yml`  | https://dev.x9bc433.win | `127.0.0.1:5173` |
| prod | `/home/sam/funnelmanager-prod` | `funnelmanager-prod` | `docker-compose.prod.yml` | https://x9bc433.win     | `127.0.0.1:8080` |

Prod publishes only to loopback (`HOST_BIND=127.0.0.1:`) and on non-conflicting
ports (`PROD_HTTP_PORT=8080`, `PROD_MCP_PORT=8013`, `PROD_OPENCLAW_PORT=18799`)
so it never clashes with dev or bypasses ufw. Volumes are namespaced by the
`funnelmanager-prod` project (pinned via `name:` in the compose file), separate
from dev's `*_dev` volumes.

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

## Memory posture (post-incident, 2026-07-17)

The box froze once under memory-pressure livelock (thrash without an OOM kill).
Defenses now in place:

- 8GB swapfile (`/swapfile`, in fstab) on top of the 496MB Linode swap partition.
- Every container carries a `mem_limit` in both compose files, so a runaway
  container gets OOM-killed inside its cgroup (and restarted by its restart
  policy) instead of stalling the host.
- Both mongods run `--wiredTigerCacheSizeGB 0.25` — the default assumes ~50% of
  host RAM *per mongod*, which two coexisting stacks cannot afford.
- Milvus is capped at 1.5G; it reads its cgroup limit and scales its internal
  watermarks accordingly.
