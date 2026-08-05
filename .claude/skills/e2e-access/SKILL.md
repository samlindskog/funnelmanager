---
name: e2e-access
description: Sanctioned, least-privilege elevation + token minting for the e2e-canary test identity, to drive a BACKEND API directly as a real user when no UI can hand over a token (pre-UI phases, headless E2E). Use when you need an e2e-canary bearer token to call /api/<svc> in a test. Elevate into an existing role group, mint a short-lived token, run, then always revoke. NOT for driving a browser flow (use drive-canary) or for real user/role admin.
---

# e2e-access

The **token-and-privilege** half of E2E: hands you a short-lived `e2e-canary`
bearer token with exactly the roles a test needs, then takes them away. It exists
because a backend API test (before that service's UI is built, or headless) needs
a real user token, and neither the browser (token extraction is sandboxed) nor any
prod client (no password grant) will hand one over.

**Doctrine — least privilege, always reversible.** `e2e-canary` is **dormant** by
default (no access roles, no way to mint a token). A test **elevates** it into an
existing role group AND opens a token-mint window, **mints**, runs, then **revokes**
both — even on failure. Every elevation is an explicit, human-confirmed, auditable
action.

## The mechanism (built once by `init`)

- A dedicated **confidential** Keycloak client `e2e-driver` (NOT a prod-facing
  client): `directAccessGrantsEnabled` is **off** by default and only flipped on
  inside a mint window; carries `oidc-audience-mapper`s for `agents/search/jobs/mail`
  so its tokens name the services under test. Its secret + the `e2e-canary` password
  live in the in-cluster Secret `identity/fm-e2e-driver` — never in the repo or on
  local disk.
- Elevation reuses the realm's **existing role groups**: `standard` = {search, mail}-access,
  `power` = {search, jobs, agents, mail}-access, `admins` = admin.

All privileged work runs on the control-plane host (`FM_CP_HOST`, default `usfr4`)
via `kcadm` inside the keycloak pod. The prod realm is live-managed (imported once),
so these structures persist in `kc-db`; re-running `init` is idempotent.

## The loop (agent-driven, human-confirmed)

For each test:

1. **State + confirm.** Announce the test and the **exact** capability it needs —
   *"drive `POST /api/agents/sessions` → needs `agents-access` → elevate `e2e-canary`
   into group `power`"* — and get the human's explicit confirmation (AskUserQuestion).
   Request the **narrowest** group that covers the test (`standard` before `power`;
   never `admins` unless the test genuinely needs it).
2. **Elevate:** `e2e.sh elevate <group>` — adds `e2e-canary` to the group + opens the
   mint window.
3. **Mint:** `e2e.sh mint` — writes a short-lived (~mins) token to `/tmp/e2e_token`
   on the CP host and prints its `aud`/`azp`/roles. Confirm `aud` includes the target
   service before using it.
4. **Test:** call the API through the gateway with the token (and the `fm_debug=<secret>|canary`
   cookie to hit a canary), e.g.
   `ssh $FM_CP_HOST 'curl -sS -H "Authorization: Bearer $(sudo cat /tmp/e2e_token)" -H "Cookie: fm_debug=<secret>|canary" https://<host>/api/agents/sessions'`.
5. **Revoke:** `e2e.sh revoke` — removes `e2e-canary` from all elevation groups,
   closes the mint window, deletes the token. **Run this even if the test failed.**

`e2e.sh status` shows the current roles/groups + mint-window state — check it's
dormant when you're done.

## Verbs

```bash
.claude/skills/e2e-access/e2e.sh init            # one-time: create e2e-driver, store secrets, de-privilege e2e-canary
.claude/skills/e2e-access/e2e.sh elevate <group> # add e2e-canary to standard|power|admins + open mint window
.claude/skills/e2e-access/e2e.sh mint            # mint a short-lived token -> /tmp/e2e_token on the CP host
.claude/skills/e2e-access/e2e.sh revoke          # remove from all groups + close mint window + drop token
.claude/skills/e2e-access/e2e.sh status          # show e2e-canary roles/groups + mint-window state
```

## Prerequisites

- `~/.config/fm-e2e/creds.env` with `FM_E2E_PASS` (seeded into the cluster Secret by `init`).
- `~/.config/fm-ops/env` with `FM_CP_HOST` (default `usfr4`), reachable by SSH with `sudo -n kubectl`.
- **A scoped permission rule** so the agent may run this skill after the human confirms
  the elevation (the privileged Keycloak writes are otherwise gated). Add to settings:
  `Bash(bash /Users/slindskog/projects/funnelmanager/.claude/skills/e2e-access/e2e.sh:*)`.

## Guardrails (P4 / least-privilege)

- **Narrowest group, shortest window.** Elevate to the least group that works;
  revoke immediately after; never leave `e2e-canary` elevated between tests.
- **Read-mostly, budget-bounded** (the drive-canary rule): never trigger a large
  Apollo ingest / big search / anything that spends real credits at scale.
- **The token is a secret** — it lives only in `/tmp/e2e_token` on the CP host and is
  deleted on `revoke`; never print it in full or commit it.
- `e2e-canary` is never `admin` outside a test that explicitly needs it and you
  explicitly confirmed.

## See also

- **drive-canary** — the browser/RUM half (Playwright as `e2e-canary` through the UI).
  Use that once a service has a UI; use **e2e-access** for headless/pre-UI backend tests.
- **canary** — activate/retire the `<svc>-canary` this drives against.
