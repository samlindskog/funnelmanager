---
name: observe-grafana
description: Read the funnelmanager platform's telemetry (Loki logs, Tempo traces, Prometheus metrics) through the Grafana MCP to debug a request end-to-end, verify canary telemetry, or gate a canary promotion. Use when asked to look at logs/traces/metrics, find a trace by trace-id/session/testid, check what a canary request did, confirm RUM landed, compare canary vs stable error-rate/latency, or pivot a metric spike to the log line. Read-only — it never deploys, edits, or drives a browser (use drive-canary to produce the trace, deploy-funnelmanager/prod-health to act).
---

# observe-grafana

The **read half** of the canary debugging loop: a **query cookbook** for the
platform's telemetry, executed through the Grafana MCP (`mcp__grafana__*` tools).
It does **not** run anything itself — the MCP tools are the executor; this skill is
the set of *canonical LogQL/TraceQL/PromQL joins* to hand them, plus the discipline
for using them well. Pair it with **drive-canary** (which produces the trace to
chase) and **prod-health** (cluster-side status). Full query reference:
`.claude/skills/observe-grafana/queries.md`.

## When to use

- **Debug one request end-to-end** — you have a `trace_id` (from a `traceparent`
  drive-canary captured, or a log line) and want the full backend log + trace + the
  Faro RUM user-action that started it.
- **Verify canary telemetry** — confirm a canary request actually rode the canary
  pods and was fully traced (`variant="canary"`, `app_version=~"canary-.*"`).
- **Trace a stable-prod request** — a `drive-canary --target prod` run (fm_debug
  only, prod-tracing shim) forces a sampled backend trace on **stable** pods. Such a
  trace carries **backend spans only** — prod SPAs ship no Faro, so there is **no RUM
  / browser span** and `variant="stable"`. Chase it by its `trace_id` (from
  `window.__fm_last_trace_id`) through the Loki backend join + Tempo, not the Faro
  streams.
- **Gate a promotion** — compare `fm_http_*` success-rate / P99 latency for
  `variant="canary"` vs stable before promoting (the `variant` metric label only
  populates once backends redeploy with the fm_runtime change that added it; until
  then gate on the Loki `variant` field, which is stamped immediately).
- **Find a user-action by testid** — a Phase-3 `data-testid` is both the Faro event
  name and the Playwright selector; look up what a click did.

## The datasources

Prod Grafana is `https://grafana.x9bc433.win`; the MCP talks to it with a **Viewer**
service-account token and `--disable-write`. Three datasources back the joins —
**discover their live uids first** with `mcp__grafana__list_datasources` rather than
hard-coding, because uids can differ from the type name:

- **Loki** (logs) — uid is typically `loki`. Backend structured logs + Faro RUM
  logs/events land here.
- **Tempo** (traces) — spans from the Istio mesh (5% forensic baseline) plus every
  Faro-originated `sampled=1` canary trace. Loki `derivedFields` link a `trace_id`
  straight to it.
- **Prometheus** (metrics) — the `fm_http_*` request series, labeled
  `{service, method, route, status, variant}`. `variant` (`stable` | `canary`) was
  added to the metrics to mirror the Loki `variant` log label; it appears only on
  series from a redeployed backend (pods must roll), so a missing split means the
  fleet hasn't rolled yet, not that the canary is idle. Note the code label is
  `status`, not `code`.

## Discipline (do this every time)

- **`query_loki_stats` before any broad query.** Loki bills on bytes scanned; check
  the stream/volume first, then narrow. Never run a bare `{service_name=~".+"}`.
- **Always bound the time window.** Default to `now-15m`; widen deliberately
  (`now-1h`, `now-6h`) only when a first pass is empty. An unbounded query over a
  busy stream is slow and can time out the MCP call.
- **Timestamps are UTC.** The MCP interprets offset-less times as UTC (per its own
  note); prefer relative syntax (`now-15m`) and read span/log times as UTC.
- **Start narrow, widen on empty.** Add a stream selector *and* a line filter before
  a `| json` / `| logfmt` parse; a label match is cheaper than a parsed field match.
- **Pivot, don't re-query.** Once you have a `trace_id`, follow the derivedField into
  Tempo and the tracesToLogs link back — don't fish for the same request by text in
  three datasources independently.

## The auth model (know where this sits)

The Grafana MCP is **outside the OPA/Keycloak request model** entirely: a read-only
(`--disable-write`) **Viewer** token, reaching Grafana via a scoped gateway
EnvoyFilter that exempts the `grafana.x9bc433.win` SNI from mesh JWT validation. It
can read **all** trace/log/metric data (Grafana has no per-datasource RBAC), so treat
it as a privileged read surface — never a place to mutate, and never confuse its
Viewer identity with a product principal. See MEMORY
`observability-canary-agent-program` for the full posture and accepted residuals.

## See also

- **drive-canary** — the write half: drives the canary in a headless browser and
  hands you the `traceparent` / `trace_id` these queries chase.
- **prod-health** — cluster-side pod/flux/CI status when telemetry alone isn't enough.
- `.claude/skills/observe-grafana/queries.md` — the copy-paste joins.
