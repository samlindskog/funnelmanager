---
name: e2e-access
description: Unified, least-privilege escalation/deescalation for the e2e-canary test identity, used for BOTH the browser driver and driving a backend API directly. Elevate e2e-canary into an existing role group via the dedicated e2e-escalator identity, obtain a token through the NORMAL browser flow (Playwright PKCE), drive, then always deescalate. Use whenever a test needs e2e-canary to hold a role it lacks. NOT for real user/role admin.
---

# e2e-access

The **escalation** half of E2E: gives `e2e-canary` exactly the roles a test needs
for a bounded window, then takes them away. Pairs with **drive-canary** (which owns
the browser). One mechanism serves both driving modes, and the **token always comes
from the normal browser login** — no special grant client.

**Doctrine — least privilege, always reversible.** `e2e-canary` is **dormant** by
default (no access roles). A test **escalates** it into an existing role group,
gets a token via the browser, drives, then **deescalates** — even on failure. Every
escalation is an explicit, human-confirmed, auditable action, performed by a
dedicated scoped identity (not the master admin).

## The mechanism (built once by `init`)

- **`e2e-escalator`** — a dedicated Keycloak **service account** holding standing,
  scoped `realm-management:manage-users` (can toggle group membership; NOT full
  admin). Its secret lives in the in-cluster Secret `identity/fm-e2e-escalator`.
  `escalate`/`deescalate` authenticate **as `e2e-escalator`** — so routine role
  changes are least-privilege and audited under it, and the master admin is used
  only for one-time `init`.
- Escalation reuses the realm's **existing role groups**: `standard` = {search,
  mail}-access, `power` = {search, jobs, agents, mail}-access, `admins` = admin.
- **The token is NOT minted by this skill.** After escalation, `e2e-canary` holds
  the roles, so a normal Playwright login (auth-code + PKCE via the `frontend`
  client, which already carries the `aud-agents`/`aud-search`/`aud-mail` mappers)
  yields a token good for both the browser UI and the backend API.

All privileged work runs on the control-plane host (`FM_CP_HOST`, default `usfr4`)
via `kcadm` inside the keycloak pod. The prod realm is live-managed, so `init` is
idempotent and persists in `kc-db`.

## The unified loop (agent-driven, human-confirmed)

For each test:

1. **State + confirm.** Announce the test and the **exact** capability it needs —
   *"drive `POST /api/agents/sessions` → needs `agents-access` → escalate `e2e-canary`
   into group `power`"* — and get explicit confirmation (AskUserQuestion). Request
   the **narrowest** group (`standard` before `power`; `admins` only when truly needed).
2. **Escalate:** `e2e.sh escalate <group>` (runs as `e2e-escalator`).
3. **Get the token — normal browser flow (Playwright), same for both modes:**
   log in as `e2e-canary` (drive-canary's cookie + Keycloak sign-in). Then either:
   - **Browser driving:** keep driving the UI with Playwright (this IS drive-canary).
   - **Backend driving:** capture the `Authorization: Bearer <token>` request header
     from any `/api/*` call the SPA makes (via `browser_network_requests` →
     `browser_network_request`, the same way drive-canary reads `traceparent`), then
     call the backend directly, e.g. on the CP host:
     `curl -H "Authorization: Bearer <token>" -H "Cookie: fm_debug=<secret>|canary" https://<host>/api/agents/sessions`.
     (If Playwright redacts the `Authorization` header, read it from `localStorage`
     `fm_oidc_*` via `browser_evaluate` — needs its own permission.)
4. **Deescalate:** `e2e.sh deescalate` — removes `e2e-canary` from all role groups.
   **Run this even if the test failed.**

`e2e.sh status` shows the current roles/groups + whether the escalator exists —
confirm `e2e-canary` is dormant when done.

## Verbs

```bash
.claude/skills/e2e-access/e2e.sh init               # one-time: create e2e-escalator (manage-users), store its secret, de-privilege e2e-canary
.claude/skills/e2e-access/e2e.sh escalate <group>   # add e2e-canary to standard|power|admins (as e2e-escalator)
.claude/skills/e2e-access/e2e.sh deescalate         # remove e2e-canary from all role groups (as e2e-escalator)
.claude/skills/e2e-access/e2e.sh status             # show e2e-canary roles/groups + escalator presence
```

## Prerequisites

- `~/.config/fm-ops/env` with `FM_CP_HOST` (default `usfr4`), reachable by SSH with `sudo -n kubectl`.
- Playwright + drive-canary wired (for the browser login that yields the token); `~/.config/fm-e2e/creds.env` for the `e2e-canary` login.
- **A scoped permission rule** so the agent may run this skill after the human confirms:
  `Bash(bash /Users/slindskog/projects/funnelmanager/.claude/skills/e2e-access/e2e.sh:*)`.

## Guardrails (P4 / least-privilege)

- **Narrowest group, shortest window.** Escalate to the least group that works;
  deescalate immediately; never leave `e2e-canary` escalated between tests.
- **`admins` is rare** — only for a test that genuinely needs it and that you
  explicitly confirmed (and `manage-users` may not even be able to grant it).
- **Read-mostly, budget-bounded** (the drive-canary rule): never trigger a large
  Apollo ingest / big search / anything spending real credits at scale.
- **The token is a secret** — never print it in full or commit it; it expires on its
  own (short-lived) and the roles are gone after `deescalate`.

## TODO (future)

- **Per-service escalation groups** — add `e2e-agents` / `e2e-search` / `e2e-mail` /
  `e2e-leads` / `e2e-jobs`, each granting only that one service's `-access`, so a
  test can hold *exactly* one MCP surface. This isolates per-service testing AND
  structurally prevents side effects (e.g. an agents-only test can't reach mail →
  can't send email), rather than relying on `power` (which grants all four) + a
  read-only-goal convention. Until then, keep the test goal narrow and never invoke
  a service you didn't intend (e.g. no mail tools when testing agents).

## See also

- **drive-canary** — the browser half (Playwright as `e2e-canary`); this skill just
  escalates first and yields the token drive-canary/backend curls then use.
- **canary** — activate/retire the `<svc>-canary` this drives against.
