---
name: verify-deploy
description: Telemetry PROMOTION GATE for a funnelmanager canary — given an ACTIVE canary and a trace it produced, assert via the Grafana MCP that (1) a complete multi-hop Tempo trace exists (browser→ingress→<svc>-canary→upstreams, the honor-incoming-sampled invariant), (2) there are ZERO variant=canary error/exception log lines, (3) the RUM user-action joins to that backend trace_id, and (4) canary vs stable success-rate/P99 are within tolerance. Use when asked to verify/gate/qualify a canary for promotion, check a canary is healthy end-to-end, confirm a trace is complete, or decide if a canary is safe to promote. Read-only cookbook (agent-run Grafana-MCP queries) — it NEVER deploys, promotes, retires, or writes.
---

# verify-deploy

The **promotion gate** for the funnelmanager canary: the read-only telemetry
assertions that decide *is this canary safe to promote?* It sits on top of three
siblings and re-implements none of them:

- **canary** activates/retires the `<svc>-canary` workload and reports its state.
- **drive-canary** exercises the live canary (headless Playwright as `e2e-canary`)
  and hands you the `traceparent` / `trace_id` a click produced.
- **observe-grafana** owns the raw LogQL/TraceQL/PromQL *primitives* (query cookbook
  + datasource-uid discovery + Loki-stats discipline). This skill is the **gate
  recipe** built from them — the specific joins + pass/fail criteria for promotion.

Everything here is executed through the **Grafana MCP** (`mcp__grafana__*`). A shell
driver cannot call an MCP tool, so the optional helper only **prints** the
parametrized queries for the agent to run; there are **no prod writes** anywhere in
this skill. Helper: `.claude/skills/verify-deploy/gate.sh <svc> [trace_id]`.

## When to use

- Gate a promotion: you drove a canary, have a `trace_id`, and must decide pass/fail.
- Confirm a canary rode the **canary pods end-to-end** and was fully traced.
- Prove a change did not add errors or regress latency vs stable before promoting.

## Prerequisites

- The target `<svc>-canary` is **ACTIVE** (`canary` skill `list` shows `replicas: 1`
  + route/VS listed). An idle canary falls through to **stable**, so a "green" gate
  against an idle canary is meaningless — check activation first.
- A `trace_id` produced by driving the canary (**drive-canary** step 5). Without one
  you can still run the aggregate checks (2 & 4) but not the per-request checks (1 & 3).
- Grafana MCP **Connected** and read-only (Viewer token, `--disable-write`). Run
  `mcp__grafana__list_datasources` first to resolve the Loki / Tempo / Prometheus uids
  (do not hard-code them), and `mcp__grafana__query_loki_stats` before any broad Loki
  query. Bound every query to a window (`now-15m` default, UTC).

## The four gate assertions

Run all four; a promotion PASSES only if every one passes. Swap `<svc>` for the
canaried service and `<trace_id>` for the drive-canary trace.

### 1. Complete multi-hop trace (honor-incoming-sampled invariant)
The canary SPA's Faro originates a `sampled=1` `traceparent`, and every sidecar
honors it — so a canary request must appear as **one** trace spanning
**browser (Faro) → istio-ingress → `<svc>-canary` → its stable upstreams**. Fetch it:
```traceql
{ trace:id = "<trace_id>" }
```
**PASS:** a single trace id with spans crossing the ingress into the `-canary`
workload and on to the upstream service(s) it called. **FAIL:** the trace stops at the
ingress (sidecar didn't honor `sampled=1`), never reaches `-canary` (routed to stable
— canary idle or cookie missing), or the multi-hop chain is broken. Pivot span→logs
with `tracesToLogs` to see where it dead-ends.

### 2. Zero canary error / exception log lines (Loki)
Every canary log line carries `variant="canary"` (from the per-pod
`FM_DEPLOYMENT_VARIANT`) and canary-request lines also carry `canary="true"`. Scan
all backends for error/critical over the run window:
```logql
{service_name=~"search|leads|mail|mcp|jobs|agents"} | json | variant="canary" | level=~"error|critical"
```
**PASS:** zero lines. **FAIL:** any error/critical line — read it (and its `trace_id`)
before promoting. (Run `query_loki_stats` first; narrow to `{service_name="<svc>"}`
if the fan-out is noisy.)

### 3. RUM user-action → backend trace_id join
Prove the browser click that started the flow is telemetry-joined to the backend
trace. The Faro user-action name is the clicked control's `data-testid`:
```logql
{service_name="faro"} | logfmt | event_data_userActionName="<testid>"
```
Read its `trace_id`, then confirm the backend saw the same id:
```logql
{service_name=~"search|leads|mail|mcp|jobs|agents"} |= "<trace_id>"
```
**PASS:** the Faro action's `trace_id` matches the drive-canary `traceparent` and
resolves to backend lines on the `-canary` pods. **FAIL:** no RUM event (telemetry
didn't init — the dynamic-import race; use a post-settle click), or the ids don't
join (you traced a different request).

### 4. Canary vs stable — success-rate + P99 (PromQL)
Compare the `fm_http_*` series split by `variant`. **NOTE on the label:** the
`variant` label is being ADDED to `fm_http_*` in a **parallel workstream** — until
those images redeploy the split may be empty. Write the PromQL with the `variant`
label (it starts working post-release); if empty, fall back to the **pod label** the
series already carries (`pod=~"<svc>-canary-.*"` vs the stable pod), and confirm the
available labels with `mcp__grafana__list_label_values` before charting.

Success-rate by variant:
```promql
sum by (variant) (rate(fm_http_requests_total{service="<svc>",code!~"5.."}[10m]))
  / sum by (variant) (rate(fm_http_requests_total{service="<svc>"}[10m]))
```
P99 latency by variant:
```promql
histogram_quantile(0.99,
  sum by (le, variant) (rate(fm_http_request_duration_seconds_bucket{service="<svc>"}[10m])))
```
Pod-label fallback (until the `variant` label ships) — canary only:
```promql
sum(rate(fm_http_requests_total{service="<svc>",pod=~"<svc>-canary-.*",code!~"5.."}[10m]))
  / sum(rate(fm_http_requests_total{service="<svc>",pod=~"<svc>-canary-.*"}[10m]))
```
**PASS:** canary success-rate ≥ stable (within noise) and canary P99 not materially
worse than stable. **FAIL:** a lower success-rate or a P99 regression — do not promote.

## East-west (internal) canaries — a FIRST-CLASS drive/verify step

Internal canaries (`leads`, `mcp`, `jobs`) have **no edge route**, so they cannot be
loaded directly in a browser. Drive one by exercising a **gateway** canary whose
internal hop lands on it, then verify by the propagated marker — this step is now
first-class, not just a printed hint:

1. **Activate** both the internal canary AND a gateway canary that calls it (for
   `leads`: `search-canary` must be active, since `search→leads` is the hop).
2. **Drive** a cookie-gated canary flow via **drive-canary** (e.g. a cheap search).
   `fm_runtime` propagates `x-fm-canary` across the `search→leads` hop, so the request
   lands on `leads-canary`.
3. **Verify it actually rode the internal canary** (assertion 1/2 applied in-mesh):
   ```logql
   {service_name="leads"} | json | canary="true"
   ```
   and confirm the same `trace_id` spans `search-canary → leads-canary` in Tempo
   (assertion 1). **PASS:** internal-canary lines carry `canary="true"` under the drive
   trace id; **FAIL:** the internal hop shows `variant="stable"` (marker didn't
   propagate, or the internal canary is idle so the hop fell through to stable).

## No writes — where this sits

This skill only **reads** telemetry to render a pass/fail verdict. It never promotes,
retires, reconciles, or edits — acting on the verdict is the **canary** /
**deploy-funnelmanager** skills' job. The Grafana MCP is a privileged read-only
surface (Viewer token, outside the OPA/Keycloak request model); never confuse its
identity with a product principal, and never mutate through it.

## See also

- **observe-grafana** — the raw query primitives + datasource discovery this gate uses.
- **drive-canary** — produces the `trace_id`/`traceparent` assertions 1 & 3 chase.
- **canary** — activate/retire the workload; confirm ACTIVE before gating.
- **prod-health** — cluster-side pod/flux status when telemetry alone isn't enough.
