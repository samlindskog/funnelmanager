---
name: deploy-funnelmanager
description: Deploy funnelmanager to production — commit and push, tag a release, watch CI build/pin images, Flux rollout to the k3s cluster on the Linode VPSs, verify, rollback, Cloudflare purge. Use when asked to deploy, release, ship, push to prod, roll back, or propagate changes to the VPSs.
---

# Deploy funnelmanager

The **release/rollback engine** for funnelmanager production. Its one job is to
*act* on prod (tag → watch CI → reconcile → wait rollout → rollback → purge); all
*health/verification* is delegated to the **prod-health** skill (`check.sh`), so
`release`'s post-rollout verify, `status`, and `smoke` all shell out to it rather
than re-implementing curl/flux checks. Use **prod-health** to look, this to act,
and **ship-branch** for the full review→ship→verify arc.

This skill releases the **stable** prod track only — it never touches the
`*-canary` workloads. Building, activating, and retiring a telemetry-enabled
canary is owned by the separate **`canary`** skill (which delegates its rollout
back to this skill's `reconcile`/health path). A normal release here leaves any
active canary untouched.

All paths are relative to the repo root. The driver is
`.claude/skills/deploy-funnelmanager/deploy.sh`.

**The model is GitOps — a deploy is a git commit, never an ssh push.**
`release-prod` (GitHub Actions) builds the images (ten: the nine deployed
services plus `backup`) to `ghcr.io/samlindskog/funnelmanager/<svc>`, then commits
a `sha-<sha>` pin into `deploy/apps/overlays/prod/kustomization.yaml` on `main`
(`[skip ci]`). Flux on the k3s control plane (**usfr4**; usfr2 runs `k3s-agent` —
the old compose-prod there is retired) reconciles `main` and rolls out the `prod`
namespace behind Cloudflare at https://x9bc433.win.

## Prerequisites

Already present on this machine: `git`, `gh` (authed), ssh alias `usfr4` in
`~/.ssh/config` (user `sam`, admin key). Nothing to install.

## Deploy to production (agent path)

```bash
# 1. Commit the work and push main as usual (plain pushes do NOT deploy).
git add -A && git commit -m "..." && git push

# 2. Release — does everything: preflight, tag, watch CI, pull the pin
#    commit, force Flux reconcile, wait for rollout, public smoke.
.claude/skills/deploy-funnelmanager/deploy.sh release v1.3.0
```

`release` refuses to run off-main, with a dirty tree, or when local `main`
diverges from origin. After the rollout wait it runs the prod-health `drift` +
`smoke` verifier. Other entry points:

```bash
.claude/skills/deploy-funnelmanager/deploy.sh status          # local git + prod-health drift/flux/ci
.claude/skills/deploy-funnelmanager/deploy.sh smoke           # prod-health public checks, no ssh
.claude/skills/deploy-funnelmanager/deploy.sh watch           # attach to an in-flight release
.claude/skills/deploy-funnelmanager/deploy.sh rollback v1.1.0 # re-release an older ref
.claude/skills/deploy-funnelmanager/deploy.sh purge https://x9bc433.win/favicon.svg
```

`status`, `smoke`, and the release verify call `.claude/skills/prod-health/check.sh`
under the hood — there is no duplicate health logic here.

Rollback alternative: revert the pin commit on `main` — Flux re-reconciles
either way.

## What the driver runs underneath (verified 2026-07-23, v1.2.0 release)

```bash
git tag v1.2.0 && git push origin v1.2.0          # triggers release-prod
gh run watch <run-id> --exit-status --interval 15  # ~2.5 min
git pull origin main                               # fetch CI's pin commit
ssh usfr4 'sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux reconcile source git flux-system -n flux-system'
ssh usfr4 'sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux reconcile kustomization apps-prod'
# wait every prod Deployment (frontend mailui agentsui search leads mail mcp jobs agents):
ssh usfr4 'for d in frontend mailui agentsui search leads mail mcp jobs agents; do sudo -n kubectl -n prod rollout status deploy/$d --timeout=300s; done'
.claude/skills/prod-health/check.sh drift          # running sha == pinned sha on every deploy
.claude/skills/prod-health/check.sh smoke          # 200 hub + whoami 403 (OPA deny) = healthy
```

## Gotchas (all hit for real)

- **Local `main` is behind after every release.** CI pushes the
  `ci(prod): pin images to sha-… [skip ci]` commit to `main`. Always
  `git pull` before the next commit or your push gets rejected. The driver
  pulls automatically inside `watch`.
- **`flux`/`kubectl` on usfr4 need root's kubeconfig.** Bare `flux reconcile`
  fails with `dial tcp [::1]:8080: connect: connection refused`. Use
  `sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux …` (baked into the
  driver).
- **Unauthenticated `/api/*/whoami` returns 403, not 401** — OPA default-deny
  answers before auth challenges. 403 is the healthy signal; don't "fix" it.
- **Cloudflare API calls from usfr4 must be `curl -4`.** Over IPv6 the scoped
  token fails with "Cannot use the access token from location". The token
  (secret `cloudflare-api-token`, ns `cert-manager`) is DNS/purge-scoped:
  `/zones` listing returns auth errors — that's expected; the zone id for
  x9bc433.win is `8303098fbf6f966ac44b6f2945e9d732`.
- **Static assets are edge-cached (max-age 14400).** After changing favicons
  or other long-cached assets, `deploy.sh purge <url…>` or users see stale
  copies for up to 4h (`cf-cache-status: HIT`).
- **Plain pushes to `main` never deploy.** Only `v*` tags or a manual
  `workflow_dispatch` trigger `release-prod`. CI (`ci.yml`) still runs
  checks on every push.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `gh run watch` exits non-zero | `gh run view <id> --log-failed`; nothing was deployed (pin commit only lands on success) |
| `git push origin vX.Y.Z` rejected / tag exists | you re-used a tag — bump the version; tags are immutable releases |
| `push` to main rejected (non-fast-forward) | the CI pin commit landed — `git pull` then push |
| `flux … connection refused` on usfr4 | missing `sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml` prefix |
| Cloudflare "Invalid API Token" / "access token from location" | add `-4` to curl; use the hardcoded zone id |
| Rollout hangs on one deploy | `ssh usfr4 'sudo -n kubectl -n prod describe pod <pod>'` — usually an image pull or probe failure; `deploy.sh rollback <last-good-tag>` |

## Self-improvement hook

`.claude/settings.json` registers a `SessionEnd` hook that runs
`improve-skill.sh`: after any session whose transcript shows deploy activity,
it spawns a headless `claude -p` (file-edit tools only) to fold new errors,
workarounds, or commands from that session back into this skill. Log:
`.claude/skills/deploy-funnelmanager/improve.log`.
