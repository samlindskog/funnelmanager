# observe-grafana — query cookbook

Canonical LogQL / TraceQL / PromQL joins for the funnelmanager telemetry stack, to
hand to the Grafana MCP (`mcp__grafana__*`). **Run `mcp__grafana__list_datasources`
first** to resolve live datasource uids (Loki is usually `loki`; Tempo/Prometheus
discover live). **Run `mcp__grafana__query_loki_stats` before any broad Loki query**,
and bound every query to a window (`now-15m` default; UTC).

## Telemetry shape (ground truth — the labels these joins rely on)

- **Backend logs** (`fm_runtime` structured JSON, one stream per service:
  `{service_name="search"}`, `leads`, `mail`, `mcp`, `jobs`, `agents`):
  - Step-1 stamps `variant` on **every** log line (`stable` | `canary`, from the
    per-pod `FM_DEPLOYMENT_VARIANT`).
  - Canary-request lines additionally carry `canary: true` (presence-only marker,
    propagated across every internal hop via the `x-fm-canary` trace header).
  - Every request-scoped line carries the `traceparent` (hence a `trace_id`).
- **Faro RUM** (browser, canary-only). **Don't hard-code the stream selector** —
  Faro lands under `{job="faro"}` (Alloy's default), *not* the suspect
  `{service_name="faro"}` earlier drafts assumed. **Discover it live** with
  `mcp__grafana__list_loki_label_names` then `...list_loki_label_values` for the
  candidate label (`job`, `service_name`) before querying; the value is `faro`.
  - `app.name` — **which SPA** emitted the event: one of `frontend` (hub),
    `searchui`, `mailui`, `agentsui`. Faro sets it as an event field (surfaces after
    `| logfmt` as `app_name`, or a Loki label like `app` — confirm the exact key via
    `list_loki_label_values`, don't assume). Filter on it to scope RUM to one app.
  - `event_data_userActionName=<testid>` — the Phase-3 `data-testid` of the clicked
    control (= Faro user-action name = Playwright selector).
  - `app_version=canary-<sha>` — canary bundle version (stable prod ships no Faro).
- **Tempo** — mesh spans (5% baseline) + Faro-originated `sampled=1` canary traces,
  keyed by `trace_id`. Loki `derivedFields` link `trace_id` → Tempo;
  `tracesToLogs` links a span back to its Loki lines.
- **Prometheus** — `fm_http_*` request series, labeled
  `{service, method, route, status, variant}`. The `variant` label (`stable` |
  `canary`) was **added to the metrics** to match the Loki `variant` log label, so
  canary-vs-stable metric splits work — but it only appears on series emitted by a
  backend **redeployed** with that fm_runtime change (a pod must roll to emit it).
  The Loki `variant` field works immediately (already stamped); the metric split
  lags until the fleet rolls. Note the status label is `status` (string HTTP code,
  e.g. `"200"`), **not** `code`.

---

## Loki (LogQL)

> **Faro stream selector:** the examples below use `{job="faro"}` (Alloy's default).
> **Confirm it first** — run `mcp__grafana__list_loki_label_values` for `job` (and, if
> empty, `service_name`) and use whichever label actually carries the value `faro`.
> Don't assume `{service_name="faro"}`.

### Faro user-action by testid
Find the RUM event a specific labeled control emitted:
```logql
{job="faro"} |= "<testid>"
```
Narrower (parse then match the action-name field):
```logql
{job="faro"} | logfmt | event_data_userActionName="<testid>"
```

### Faro RUM for one SPA (app.name)
Scope browser telemetry to a single app — `frontend` (hub), `searchui`, `mailui`,
`agentsui` (confirm the exact parsed key via `list_loki_label_values`; `app_name` here
is the `| logfmt` rendering of Faro's `app.name`):
```logql
{job="faro"} | logfmt | app_name="searchui"
```

### Canary RUM only
All browser telemetry from a canary bundle (excludes any stray stable data):
```logql
{job="faro"} | logfmt | app_version=~"canary-.*"
```

### Backend logs for a canary request
By the presence marker, then optionally by variant label:
```logql
{service_name="search"} | json | canary="true"
```
```logql
{service_name="search"} | json | variant="canary"
```
Swap `search` for any backend (`leads` `mail` `mcp` `jobs` `agents`) to follow the
request across hops — the `canary` marker propagates on every internal call.

### Backend logs by trace id
The workhorse pivot — every service line for one request, by its `trace_id`:
```logql
{service_name="<svc>"} |= "<trace_id>"
```
Across all backends at once (bound the window; run stats first):
```logql
{service_name=~"search|leads|mail|mcp|jobs|agents"} |= "<trace_id>"
```

### Error/critical lines for a canary (promotion gate input)
```logql
{service_name=~"search|leads|mail|mcp|jobs|agents"} | json | variant="canary" | level=~"error|critical"
```

---

## Tempo (TraceQL)

### Fetch a trace by id
```traceql
{ trace:id = "<trace_id>" }
```
Use `mcp__grafana__query_tempo` (discover the exact tool name from the proxied Tempo
tools) with the Tempo datasource uid from `list_datasources`.

### The pivots (use the built-in links, don't re-query by text)
- **Loki → Tempo:** a backend log line's `trace_id` is a Loki **derivedField** — it
  resolves straight to the Tempo trace. Get the `trace_id` from the LogQL join above,
  then open the trace.
- **Tempo → Loki (`tracesToLogs`):** from any span, pivot back to the Loki lines for
  that `trace_id` / service — this is how you go span → the structured log detail the
  span doesn't carry.
- **Metric spike → trace:** Prometheus **exemplars** on `fm_http_*` carry a
  `trace_id`; jump from a latency/error spike to the exact slow trace, then to its
  logs.

Expected canary shape: a single trace spanning browser (Faro) → istio-ingress →
`<svc>-canary` → stable upstreams, sampled because the canary SPA originated
`sampled=1` (honor-incoming-sampled at the 5% mesh baseline).

Expected **`drive-canary --target prod`** shape: NO browser/Faro span (prod ships
no RUM). The drive-canary fetch **shim** originates `sampled=1` on same-origin
`/api/*` only, so the trace starts at **istio-ingress → stable `<svc>` → upstreams**
with `variant="stable"` — **backend spans only**. Honored solely because the request
carried a valid `fm_debug` cookie (the `debug-session-gate` resets a forced
`traceparent` to baseline otherwise). Chase it by the `trace_id` the shim stashed on
`window.__fm_last_trace_id`.

---

## Prometheus (PromQL)

> **`variant` is a real metric label now** (added to `fm_http_requests_total` /
> `fm_http_request_duration_seconds` in `fm_runtime`). The split below only populates
> on series from a backend **redeployed** with that change — pods must roll to emit
> it. Until the fleet rolls, use the Loki `variant` field (stamped immediately) for
> canary-vs-stable. The status label is **`status`** (string HTTP code), not `code`.

### Success-rate by variant (promotion gate)
Split the `fm_http_*` request counter by `variant`:
```promql
sum by (variant) (rate(fm_http_requests_total{status!~"5.."}[5m]))
  / sum by (variant) (rate(fm_http_requests_total[5m]))
```

### P99 latency by variant
```promql
histogram_quantile(0.99,
  sum by (le, variant) (rate(fm_http_request_duration_seconds_bucket[5m])))
```

### Canary vs stable, one service
```promql
sum by (variant) (rate(fm_http_requests_total{service="search"}[5m]))
```

Confirm the exact metric names with `mcp__grafana__list_prometheus_metric_names` /
`...metric_metadata` (the `fm_http_*` family) and the available label values with
`...label_values` before charting — an empty `variant=canary` series means either no
canary is taking traffic, or the pods serving it **predate** the fm_runtime change
that added the label (roll them, then re-query; the Loki `variant` field is the
interim source of truth).
