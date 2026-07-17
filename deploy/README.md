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
`funnelmanager-prod` project, separate from dev's `*_dev` volumes.

The OpenClaw agent is **off in prod** (it sits behind the `agent` Compose
profile) so it doesn't run a second Telegram poller against dev's bot token.
To enable it, give prod its own `TELEGRAM_BOT_TOKEN` in `.env.prod` and run
`docker compose --profile agent -f docker-compose.prod.yml --env-file .env.prod up -d`.

## Manual deploy (on the box)

```bash
cd /home/sam/funnelmanager-prod
deploy/prod-deploy.sh              # latest origin/main
deploy/prod-deploy.sh v1.2.0      # a tag
```

## CI/CD (GitHub Actions)

- **`.github/workflows/ci.yml`** — runs on every PR and push to main: frontend
  typecheck/build/lint + Compose validation. No deploy.
- **`.github/workflows/deploy-prod.yml`** — deploys prod. Never triggered by a
  plain push to main; only by:
  - pushing a tag `v*` (`git tag v1.2.0 && git push origin v1.2.0`), or
  - the **Run workflow** button (`workflow_dispatch`), which takes a ref.

  It SSHes to usfr2 as `sam` using a dedicated, forced-command deploy key and
  runs `deploy/prod-deploy.sh <ref>` there.

### Required GitHub repo secrets

| Secret | Value |
|---|---|
| `PROD_HOST` | `192.155.85.254` |
| `PROD_USER` | `sam` |
| `PROD_SSH_KEY` | private half of the CI-only ed25519 deploy key |

The CI key's public half is installed in `~sam/.ssh/authorized_keys` on usfr2
with `command="/home/sam/funnelmanager-prod-deploy-hook.sh",no-pty,...` so that
key can *only* trigger a prod deploy — it gets no shell.

Optionally add a required reviewer to the GitHub **production** environment
(Settings → Environments) to gate each deploy behind an approval.
