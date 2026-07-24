---
name: deploy-dev
description: Ship the current code to the DEV cluster (dev namespace, https://dev.x9bc433.win) via GitOps — dispatch the deploy-dev GitHub workflow and resume the Flux apps-dev kustomization. Use when asked to deploy/preview/ship to dev, or to test a branch on the dev cluster before prod. For production, use deploy-funnelmanager instead.
---

# Deploy to dev

Dev is GitOps, same model as prod: a deploy is a workflow dispatch + Flux
reconcile, **never an ssh push**. This skill is a thin wrapper over the shared
driver `.claude/skills/deploy-funnelmanager/deploy.sh` (the `dev` subcommand) so
dev is invokable on its own. Paths are relative to the repo root.

## Deploy

```bash
# 1. Commit and push the work first (plain pushes do NOT deploy).
git add -A && git commit -m "..." && git push

# 2. Dispatch deploy-dev for a ref (defaults to main). Builds images and
#    points the dev overlay at them.
.claude/skills/deploy-funnelmanager/deploy.sh dev            # ref=main
.claude/skills/deploy-funnelmanager/deploy.sh dev my-branch  # a feature branch
```

`apps-dev` ships **suspended**, so the dispatch alone does not serve it. Resume
Flux to actually roll it out (the deploy.sh output prints the exact command):

```bash
ssh usfr4 "sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux resume kustomization apps-dev"
```

## Verify

```bash
.claude/skills/deploy-funnelmanager/deploy.sh status   # git + recent runs + flux/deploy state
```

Public dev URL: **https://dev.x9bc433.win** (Cloudflare → edge). If it doesn't
reflect the new code, check `deploy.sh status` for the Flux `apps-dev` state and
re-reconcile.

## Notes
- Requires `gh` (authed) and ssh alias `usfr4` — both already present on this machine.
- The dev realm/secrets are dev-only (admin/admin). Never point this at prod data.
- Prod deploys, rollback, and Cloudflare purge live in the `deploy-funnelmanager`
  skill — this skill is dev-only on purpose.
