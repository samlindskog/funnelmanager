---
name: drive-canary
description: Drive the telemetry-enabled funnelmanager canary (or trace stable prod) headlessly through the Playwright MCP as the e2e-canary identity — log in, seed the fm_debug=<secret>|canary cookie for a canary target (prod tracing needs no cookie — inject the tracing shim), click a labeled control by its data-testid, and capture the resulting traceparent to hand to observe-grafana. This is the agent-driven E2E loop. Use when asked to drive/exercise the canary, trace a prod request, click a button and trace it, run the E2E loop, reproduce a UI flow with telemetry, or capture a trace from a browser action. Read-mostly ONLY (P4 budget) — never triggers a large Apollo search.
---

# drive-canary

The **write half** of the canary debugging loop: it *acts* in a headless browser via
the Playwright MCP (`mcp__playwright__*`), reaching either the **telemetry-enabled
canary** or **stable prod** as the dedicated `e2e-canary` identity, clicking a control
resolved by its Phase-3 `data-testid`, and capturing the `traceparent` that click
generated. That trace id is then handed to **observe-grafana** (the read half) to see
what the click did across the stack. This *is* the CLAUDE.md "agent-drivable E2E" loop,
driven from this session. Setup driver: `.claude/skills/drive-canary/setup.sh`.

## Targets: `--target canary` (default) vs `--target prod`

The single value-encoded `fm_debug` cookie gates the debug capability AND encodes
the route selection: `fm_debug=<secret>|canary` routes to the canary, `fm_debug=<secret>`
alone to stable. So the loop has two modes (run `setup.sh --target <t>` for
target-specific wiring):

- **`--target canary`** — seed `fm_debug=<secret>|canary` via `/debug/canary/on` so
  `/api/*` routes to `<svc>-canary`. The canary bundle ships its own **Faro** (full
  RUM + browser-originated traces); capture the `traceparent` from
  `browser_network_requests` exactly as before. No shim.
- **`--target prod`** — **no cookie needed.** Just log in and inject the
  **prod-tracing shim** (below); `/api/*` requests then carry a fresh sampled
  `traceparent` the mesh **honors** (honor-incoming-sampled — the same behavior
  canary Faro relies on), routed to **stable/prod** pods. Prod SPA bundles ship
  **no Faro** (tree-shaken out), so **LIMITATION: backend spans only, no browser/RUM
  spans.** The `fm_debug` cookie does **not** gate this — the mesh honors any
  injected sampled `traceparent` (a cookie-based force-sample gate was attempted and
  removed as impossible in Envoy; see `debug-session-gate.yaml`). Safe way to trace a
  *stable prod* request end to end without a canary.

### The prod-tracing shim (inject via `browser_evaluate`)

After login + settle, inject the wrapper below with `mcp__playwright__browser_evaluate`
(print the canonical copy with `setup.sh shim`). It wraps `window.fetch` to stamp a
fresh W3C `traceparent: 00-<32 hex>-<16 hex>-01` (from `crypto.getRandomValues`) on
**same-origin `/api/*` requests ONLY** — never Keycloak / cross-origin (a custom header
there triggers a CORS preflight that breaks token refresh: the `x-fm-canary` lesson) —
and stashes the last trace id on `window.__fm_last_trace_id` for hand-off:

```js
() => {
  if (window.__fm_trace_shim) return 'already installed';
  const hex = (n) => {
    const b = new Uint8Array(n);
    crypto.getRandomValues(b);
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  };
  const origin = window.location.origin;
  const orig = window.fetch.bind(window);
  window.fetch = (input, init) => {
    try {
      const raw = (typeof input === 'string' || input instanceof URL) ? input : input.url;
      const url = new URL(raw, origin);
      if (url.origin === origin && url.pathname.startsWith('/api/')) {
        const traceId = hex(16);           // 16 bytes -> 32 lowercase hex
        const spanId  = hex(8);            //  8 bytes -> 16 lowercase hex
        const tp = `00-${traceId}-${spanId}-01`;  // sampled (-01)
        window.__fm_last_trace_id = traceId;
        init = init ? Object.assign({}, init) : {};
        const h = new Headers(init.headers ||
          (input instanceof Request ? input.headers : undefined));
        h.set('traceparent', tp);
        init.headers = h;
      }
    } catch (e) { /* never break fetch */ }
    return orig(input, init);
  };
  window.__fm_trace_shim = true;
  return 'installed';
}
```

Then read `window.__fm_last_trace_id` (via `browser_evaluate`) after the click, or the
`traceparent` header off the `/api/*` request in `browser_network_requests`, and hand
it to observe-grafana. Prod forced-sampling is honored via **honor-incoming-sampled**
— the mesh honors any injected sampled `traceparent`, cookie or not (an accepted
telemetry-cost residual; a cookie-gate was attempted and removed as impossible
in-band — see `debug-session-gate.yaml`), so `--target prod` needs no cookie.

## When to use

- Reproduce a UI flow on the canary and get its trace/logs.
- Author or self-heal a browser journey against the live canary (a11y-snapshot mode).
- Produce the `traceparent` that `observe-grafana` queries chase.

## Prerequisites & paths

- **Both MCPs connected** — `claude mcp list` must show `grafana` and `playwright`
  ✔ Connected. `setup.sh` verifies this.
- **Creds/paths in `~/.config/fm-e2e/creds.env`** (chmod 600): `FM_E2E_USER` /
  `FM_E2E_PASS` (the e2e-canary login), `FM_DEBUG_TOKEN` (the `fm_debug` secret
  cookie value, also the `?t=` for the `/debug/*` toggles), `FM_HUB_URL` (the app
  origin). Grafana access is NOT here — the Grafana MCP holds its own URL + Viewer
  token in `~/.claude.json`.
- **Playwright MCP config** `~/.config/fm-e2e/playwright-mcp.json` — **COOKIE mode**:
  headless chromium, **no `extraHTTPHeaders`**. `setup.sh` writes it. The old
  `x-fm-canary` header is now stripped by the gateway *and* broke Keycloak token
  refresh via CORS — do not re-add it. **Config changes only take effect on an
  MCP/session restart.**

## The loop

1. **Log in** — `browser_navigate` to `FM_HUB_URL`, follow the Keycloak sign-in, and
   authenticate as `FM_E2E_USER` / `FM_E2E_PASS` (`browser_type` into the KC form,
   `browser_click` submit). The hub shares the KC session via `fm_oidc_*` localStorage.
2. **Set the debug-session cookie (once)** — `browser_navigate` to the gateway toggle
   endpoint, which sets the cookie **server-side (HttpOnly)** — identical to the
   human `enter-canary` flow. A Playwright top-level navigation sends `Sec-Fetch-Dest:
   document`, so the endpoint's anti-drive-by guard passes. Pick by target:
   ```
   # --target canary  (default): fm_debug=<secret>|canary -> /api/* to <svc>-canary
   browser_navigate  https://x9bc433.win/debug/canary/on?t=<FM_DEBUG_TOKEN>
   # --target prod: SKIP this step — prod tracing needs no cookie (the shim's
   # injected sampled traceparent is honored by the mesh regardless; requests go
   # to stable pods). /debug/on exists but is now a no-op for tracing.
   ```
   The canary endpoint sets `fm_debug=<secret>|canary` (one Set-Cookie) and 302s to
   `/`, so the gateway serves the **canary bundle** and routes `/api/*` to `<svc>-canary`
   (automatic stable fallback where no canary exists). The cookie is host-only (no CORS
   preflight), which also keeps the **KC-refresh CORS fix** the old header broke.
   `.../debug/off` clears it.
   **For `--target prod`, also inject the prod-tracing shim** (see *Targets* above) after
   login+settle — prod ships no Faro, so the shim is what makes `/api/*` requests carry a
   sampled `traceparent`.
3. **Snapshot** — `browser_snapshot` (accessibility tree) exposes the `data-testid`s
   of every labeled control; pick the one to exercise.
4. **Click a labeled control** — `browser_click` it. Prefer a **deliberate,
   post-settle** click over an on-mount action (see the async-init caveat below).
5. **Capture the traceparent** — `browser_network_requests`; find the `/api/*` request
   the click issued and read its `traceparent` request header. Extract the middle
   32-hex `trace_id` field. (`--target prod`: read `window.__fm_last_trace_id` via
   `browser_evaluate` instead — the shim stashes it there.)
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
  for a validated `fm_debug=<secret>|canary` cookie; the header also broke KC refresh
  via CORS. The cookie gate (step 2) is the *only* supported
  activation path — and it fixes the KC-refresh CORS bug. The same CORS lesson is why
  the prod-tracing shim scopes its `traceparent` to same-origin `/api/*` only.
- **Async telemetry-init caveat (still applies).** Faro loads via a dynamic import, so
  the **very first on-mount fetch may miss its `traceparent`** — the telemetry chunk
  isn't ready yet. Use a **deliberate post-settle click** (wait for the page to be
  idle, then act) so the request you're tracing carries a `traceparent`.
- **Idle canary now falls through to STABLE (no longer 503).** With the
  route/VS toggle (`canary-if-exists-else-stable`), an idle `<svc>-canary`
  (`replicas: 0`) has no route, so a cookie'd request routes to **stable** — not a
  503. The trap is the inverse: a "successful" drive against an idle canary is
  silently exercising stable. Confirm the target is ACTIVE first (`canary` skill
  `list`, or `deploy-funnelmanager`), and verify `variant="canary"` in the resulting
  telemetry (via observe-grafana) before trusting the run.
- **Icon-only buttons may not emit a *named* Faro user-action** (Faro reads the exact
  pointer target). Text controls + backend traces cover the agent's paths; the testid
  still resolves the Playwright selector.

## See also

- **observe-grafana** — the read half: the LogQL/TraceQL/PromQL joins that turn the
  captured `trace_id` into the end-to-end picture.
- **canary** — activate/retire the `<svc>-canary` workload the loop targets.
