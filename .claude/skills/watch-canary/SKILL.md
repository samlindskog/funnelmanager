---
name: watch-canary
description: Have Claude WATCH your live human canary session and diagnose bugs you hit while hand-navigating the funnelmanager app. You drive the telemetry canary in your own browser (via enter-canary) and narrate what breaks ("I clicked X and it errored"); Claude finds your session in Loki/Tempo and pulls your recent user-actions, JS errors, and the full browser→backend trace around that moment to root-cause it. Use when the user wants to report/reproduce bugs interactively by navigating the app. Read-only observation via the Grafana MCP.
---

# Watch the canary (Claude observes your session)

You hand-drive the telemetry canary; Claude is the pair on the telemetry side.
Because the canary runs Faro RUM, **every click (by `data-testid`), JS error, and
browser→backend trace you generate is in Loki/Tempo** — so when you say "this
broke," Claude pivots straight to *your* session's evidence instead of guessing.

This is read-only: it uses the `mcp__grafana__*` tools (see **observe-grafana**
for the query cookbook). It never drives your browser or deploys.

## Setup (once per session)

1. You're on the canary: run **enter-canary** (sets the `fm_debug` session cookie +
   `fm_route=canary` selector) and sign in as `e2e-canary`. Confirm the red
   "CANARY · telemetry on" badge.
2. Claude identifies **your** session — the human (non-headless) canary RUM
   session, newest first:
   ```
   {service_name="faro"} | logfmt | app_version=~"canary-.*" | browser_name!="Chrome Headless"
   ```
   Grab the `session_id` (and `app_version`) from a recent line. Everything below
   filters on that `session_id` so Claude watches *you*, not the headless agent or
   other testers. (If several human sessions exist, confirm the browser/OS/viewport
   matches yours.)

## The loop

You narrate; Claude correlates. When you report a bug ("I clicked Enrich and it
spun forever" / "the page went blank"):

1. **What you clicked** — recent user-actions, by testid:
   ```
   {service_name="faro"} | logfmt | session_id="<yours>" | event_name="faro.user.action"
   ```
   → the `event_data_userActionName` is the `data-testid` you hit.
2. **JS errors / exceptions** in your session around then:
   ```
   {service_name="faro"} | logfmt | session_id="<yours>" |~ "exception|faro.error|unhandled"
   ```
3. **The failing request's trace** — from the action's `faro.tracing.fetch` event
   grab `traceID`, then pivot (per observe-grafana):
   - Tempo waterfall: `GET /api/datasources/proxy/uid/tempo/api/traces/<traceID>`
     (via `mcp__grafana__grafana_api_request`) → browser → istio-ingress (+ OPA
     ext_authz) → the backend span (stable or `<svc>-canary`).
   - Backend logs by trace id: `{service_name="<svc>"} |= "<traceID>"` (canary and
     stable log under the same `service_name`; filter `| json | canary="true"` or
     `| variant="canary"` to isolate the canary pod).
4. **Root-cause** from the joined evidence (which control, which hop failed, the
   backend error/status, the exact log line) and report it back with the trace id
   so it's reproducible.

Tell the user what you found in terms of *their* action ("your `search-enrich`
click → `POST /api/search/.../enrich` → 500 in `search-canary`, trace `<id>`,
log: …"), not raw telemetry.

## Optional: proactive error watch

If asked to "keep an eye out" while they click around, use **/loop** (e.g.
`/loop 20s`) to re-run the exception query (step 2) for their `session_id` and
surface any NEW error the moment it appears, with its trace id — instead of
waiting for them to notice. Stop the loop when they're done.

## Guardrails & notes

- **Read-only / behavioral budget**: observation only. You (the human) drive; you
  hold `search-access` as `e2e-canary`, so keep to cheap/read flows — don't kick
  off a huge Apollo ingest just to generate telemetry (P4 Denial-of-Wallet).
- **Async-init caveat**: the very first on-mount fetch after a page load can miss
  its `traceparent` (Faro inits async). If a trace is missing for an action,
  re-trigger it with a deliberate click after the page settles.
- **Only the canary is traced** at the 1% prod baseline — that's why you must be
  on the canary (enter-canary) for full traces, not stable.

## See also

- **enter-canary** — get your browser onto the canary first (required).
- **observe-grafana** — the full LogQL/TraceQL/PromQL cookbook this leans on.
- **canary** — activate the `<svc>-canary` you want to exercise.
