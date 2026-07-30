---
name: ship-branch
description: Take a feature/fix branch (or current uncommitted work) safely to funnelmanager production — adversarial review → fix findings → re-review → merge to main → release → verify. Use when asked to "ship this branch", "review and deploy", "take X to prod", "land this", or after finishing a change that needs review before release. NOT for a raw health check (use prod-health) or an already-reviewed hotfix (use deploy-funnelmanager directly).
---

# Ship a branch to production

End-to-end pipeline for landing **reviewed** code on the funnelmanager k3s prod
cluster (control plane **usfr4**, Flux GitOps, https://x9bc433.win). It chains
three existing skills — **adversarial-review** (verify), **deploy-funnelmanager**
(release), **prod-health** (confirm) — with the judgment steps in between.

Prod topology: k3s on usfr4, namespaces `prod` + `identity` (Keycloak). A deploy
is a **git tag `v*`** → `release-prod` CI builds/pins `sha-<sha>` images → Flux
rolls out. Never ssh/compose. (The README's usfr2/compose topology is stale.)

## Pipeline

### 0. Scope the diff
Identify what's shipping — a branch (`feat/…`, `fix/…`) or uncommitted work.
`git diff --stat <base>..<branch>`. **If empty, stop.** Commit the work on its
branch first (add only the intended paths — `git add <dir>/`, never `-A`, so
parallel-agent or scratch files don't sweep in) so the review has a clean ref range.

### 1. Adversarial review
Run `adversarial-review <base>..HEAD` — bug-hunter + security-reviewer +
quality-reviewer in parallel, read-only. Synthesize one ranked verdict
(CONFIRMED vs PLAUSIBLE, deduped by root cause).

### 2. Fix loop (only if findings)
Hand accepted findings back to the **owning domain agent** (leads-agent,
search-agent, runtime-agent, …) to fix **on the same branch**. Then re-run just
the relevant reviewer(s) on the *fix diff* (`<prev-sha>..HEAD`). Repeat until
clean. **Do not ship with an unresolved CONFIRMED finding.** A restructured
concurrency/auth primitive warrants a full re-verify, not just a delta check.

### 3. Auth-affecting pre-flight (conditional — skip for pure app code)
If the change touches the **Keycloak realm / token exchange / client config**:
the realm is **not** deployed by the release (Keycloak is in `identity`, imports
its realm once into `kc-db`). Apply the live realm change to prod Keycloak
**FIRST** and verify — *before* the code rolls — or exchanges break for the
window. Use `kcadm` inside the pod (creds from secret `identity/keycloak-admin`;
the node can't reach the public KC URL, and `kcadm get … --fields attributes`
returns `{}` — use a full GET). Backward-compatible realm changes (e.g. enabling
a client attribute the new code will start using) are inert until the code ships,
so they're safe to apply ahead. Persist the change in source (`deploy/keycloak/`
realm files) too; the live change already survives restarts via `kc-db`.

### 4. Merge + release
```bash
git checkout main && git merge --ff-only <branch>   # or a merge commit if diverged
# CLEAN the tree — move scratch/untracked out (release refuses a dirty tree; untracked counts)
git status --short          # must be empty
git push origin main
.claude/skills/deploy-funnelmanager/deploy.sh release vX.Y.Z
```
Pick the version from `git tag --sort=-v:refname | head -1` — **patch** for a fix,
**minor** for a feature. `release` tags, watches CI build+pin (~2.5 min), forces
Flux reconcile on usfr4, waits the rollout, and smokes.

### 5. Verify (dogfood prod-health)
`deploy.sh release` already ran the prod-health `drift` + `smoke` verifier at the
end of the rollout wait; this step is the **full** verdict (adds pods, deploy
lag, log scans, KC exchange errors):
```bash
.claude/skills/prod-health/check.sh          # or: "check prod health"
```
Confirm **all prod deploys run the pinned sha**, pods ready, smoke green, no new
errors / `TOKEN_EXCHANGE_ERROR`s. For a specific fix, exercise the path that was
broken (or ask the user to) — structural "new sha is live" + functional both matter.

## Gotchas (all hit for real)
- **A transient SSH reset / `flux` CLI timeout during the release's rollout-wait is NOT a deploy failure.** By then CI has built, the pin commit landed, and Flux *applied* the revision — the pods roll regardless. **Verify with prod-health (pinned sha on every deploy) before reacting. Do NOT re-tag / re-release** on this alone (tags are immutable; you'd just bump versions for nothing).
- **Local `main` goes behind after every release** — CI pushes the `ci(prod): pin images … [skip ci]` commit. `git pull` before the next push.
- **Clean tree for preflight:** untracked files (scratch dirs, proposal files, a parallel agent's WIP) count as dirty. Move them out first.
- **`whoami` unauth → 403, not 401** (OPA default-deny) — the *healthy* signal.
- **Flux may briefly report the prior revision / "Reconciliation in progress"** right after a release even though the deploys already run the new sha — the running-sha-vs-pin check (prod-health `drift`) is the source of truth, not the Flux status line.
- **Rollback:** `deploy.sh rollback <last-good-tag>`, or revert the pin commit on main — Flux re-reconciles either way.

## What this chains
`adversarial-review` (steps 1–2) · `deploy-funnelmanager` (step 4, + rollback) ·
`prod-health` (step 5). Use those directly for their piece; use **this** for the
whole review→ship→verify arc.
