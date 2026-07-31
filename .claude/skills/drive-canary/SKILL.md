---
name: drive-canary
description: Drive the telemetry-enabled funnelmanager canary headlessly through the Playwright MCP as the e2e-canary identity — log in, seed the fm_canary cookie, click a labeled control by its data-testid, and capture the resulting traceparent to hand to observe-grafana. This is the agent-driven E2E loop. Use when asked to drive/exercise the canary, click a button and trace it, run the E2E loop, reproduce a UI flow with telemetry, or capture a trace from a browser action. Read-mostly ONLY (P4 budget) — never triggers a large Apollo search.
---

# drive-canary

The **write half** of the canary debugging loop: it *acts* in a headless browser via
the Playwright MCP (`mcp__playwright__*`), reaching the **telemetry-enabled canary**
as the dedicated `e2e-canary` identity, clicking a control resolved by its Phase-3
`data-testid`, and capturing the `traceparent` that click generated. That trace id is
then handed to **observe-grafana** (the read half) to see what the click did across
the stack. This *is* the CLAUDE.md "agent-drivable E2E" loop, driven from this
session. Setup driver: `.claude/skills/drive-canary/setup.sh`.

## When to use

- Reproduce a UI flow on the canary and get its trace/logs.
- Author or self-heal a browser journey against the live canary (a11y-snapshot mode).
- Produce the `traceparent` that `observe-grafana` queries chase.

## Prerequisites & paths

- **Both MCPs connected** — `claude mcp list` must show `grafana` and `playwright`
  ✔ Connected. `setup.sh` verifies this.
- **Creds/paths in `~/.config/fm-e2e/creds.env`** (chmod 600): `FM_E2E_USER` /
  `FM_E2E_PASS` (the e2e-canary login), `FM_CANARY_TOKEN` (the `fm_canary` secret
  cookie value), `FM_HUB_URL` / `FM_KEYCLOAK` / `GRAFANA_URL` (+ `GRAFANA_TOKEN`).
- **Playwright MCP config** `~/.config/fm-e2e/playwright-mcp.json` — **COOKIE mode**:
  headless chromium, **no `extraHTTPHeaders`**. `setup.sh` writes it. The old
  `x-fm-canary` header is now stripped by the gateway *and* broke Keycloak token
  refresh via CORS — do not re-add it. **Config changes only take effect on an
  MCP/session restart.**

## The loop

1. **Log in** — `browser_navigate` to `FM_HUB_URL`, follow the Keycloak sign-in, and
   authenticate as `FM_E2E_USER` / `FM_E2E_PASS` (`browser_type` into the KC form,
   `browser_click` submit). The hub shares the KC session via `fm_oidc_*` localStorage.
2. **Seed the canary cookie (once)** — a persistent-profile cookie can't be set from
   the static config, so seed it at runtime; it persists across the session:
   ```
   document.cookie = "fm_canary=<FM_CANARY_TOKEN>; path=/; Secure; SameSite=Lax"
   ```
   via `browser_evaluate`, then `browser_navigate`/reload. The gateway now serves the
   **canary bundle** and routes `/api/*` to `<svc>-canary` (with automatic stable
   fallback where no canary exists). This host-only cookie triggers no CORS preflight,
   which is why it also **fixes the KC-refresh CORS bug** the old header caused.
3. **Snapshot** — `browser_snapshot` (accessibility tree) exposes the `data-testid`s
   of every labeled control; pick the one to exercise.
4. **Click a labeled control** — `browser_click` it. Prefer a **deliberate,
   post-settle** click over an on-mount action (see the async-init caveat below).
5. **Capture the traceparent** — `browser_network_requests`; find the `/api/*` request
   the click issued and read its `traceparent` request header. Extract the middle
   16-hex `trace_id` field.
6. **Hand off to observe-grafana** — give it the `trace_id`; use its `queries.md`
   joins (`{service_name="<svc>"} |= "<trace_id>"`, the Tempo pivot) to see the flow.

## HARD GUARDRAIL — read-mostly ONLY (P4 behavioral budget)

`e2e-canary` holds **only `search-access`** and no enforced read-only role, so the
budget is **behavioral and non-negotiable**: **NEVER** trigger a large Apollo ingest
or a "Run Search" that spends Apollo credits / OpenAI $ — that is a P4
Denial-of-Wallet. **Self-cap to cheap, read-only controls:** the search history
toggle, `whoami`, owner filters, pagination over *already-fetched* data, hub tiles.
When in doubt about a control's cost, do not click it. This mirrors the CD-time rule
that verification runs are estimate-first and read-mostly.

## Gotchas

- **Config needs a restart.** Editing `playwright-mcp.json` (or the MCP command) does
  nothing until the MCP/session restarts — `setup.sh` reminds you.
- **Never re-add `extraHTTPHeaders: x-fm-canary`.** The gateway strips a
  client-supplied `x-fm-canary` on all ingress routes and re-injects the secret only
  from the validated `fm_canary` cookie; the header also broke KC refresh via CORS.
  The cookie gate (step 2) is the *only* supported activation path — and it fixes the
  KC-refresh CORS bug.
- **Async telemetry-init caveat (still applies).** Faro loads via a dynamic import, so
  the **very first on-mount fetch may miss its `traceparent`** — the telemetry chunk
  isn't ready yet. Use a **deliberate post-settle click** (wait for the page to be
  idle, then act) so the request you're tracing carries a `traceparent`.
- **Idle canary → 503.** If `<svc>-canary` is scaled to `replicas:0`, a cookie'd
  request 503s. Activate the target first (canary skill / `deploy-funnelmanager`).
- **Icon-only buttons may not emit a *named* Faro user-action** (Faro reads the exact
  pointer target). Text controls + backend traces cover the agent's paths; the testid
  still resolves the Playwright selector.

## See also

- **observe-grafana** — the read half: the LogQL/TraceQL/PromQL joins that turn the
  captured `trace_id` into the end-to-end picture.
- **canary** — activate/retire the `<svc>-canary` workload the loop targets.
