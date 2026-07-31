#!/usr/bin/env bash
# funnelmanager verify-deploy helper — PRINT the promotion-gate queries for a
# canary, parametrized by service (+ optional trace id). It executes NOTHING and
# writes NOTHING: a shell can't call the Grafana MCP, so it emits the exact
# LogQL/TraceQL/PromQL for the AGENT to run via `mcp__grafana__*`. Read-only, no
# prod writes — the whole skill is a cookbook (see SKILL.md).
#
# Usage: .claude/skills/verify-deploy/gate.sh <svc> [trace_id] [testid]
#   <svc>      the canaried service (e.g. search, searchui, leads)
#   trace_id   a trace produced by drive-canary (enables the per-request checks)
#   testid     the clicked control's data-testid (enables the RUM-join check)

set -uo pipefail   # NOT -e: a missing optional arg must not abort the print

# Shared FM_SERVICES + ops env (unused here except for service validation). See
# .claude/skills/_lib/common.sh.
. "$(cd "$(dirname "$0")/../_lib" && pwd)/common.sh"

svc=${1:-}
trace=${2:-}
testid=${3:-}
[ -n "$svc" ] || { sed -n '11,15p' "$0"; exit 1; }

# Routing hint — internal services are east-west (no edge route); everything else
# is edge-reachable via the gateway. (Mirrors canary.sh's routing taxonomy; kept
# as a hint only, so this helper stays a pure printer.)
case "$svc" in
  leads|mcp|jobs) routing=eastwest ;;
  *)              routing=gateway ;;
esac

# Backends whose logs carry variant/canary + trace_id (for the fan-out scans).
BACKENDS_RE='search|leads|mail|mcp|jobs|agents'

echo "############ verify-deploy gate queries for '${svc}-canary' (routing=${routing}) ############"
echo "Run each via the Grafana MCP (mcp__grafana__*). list_datasources first; bound to now-15m (UTC);"
echo "query_loki_stats before any broad Loki query. A promotion PASSES only if all four pass."
echo

echo "== 1. Complete multi-hop trace (honor-incoming-sampled) =="
if [ -n "$trace" ]; then
  echo "  TraceQL: { trace:id = \"$trace\" }"
  echo "  PASS: one trace id spanning browser(Faro) -> istio-ingress -> ${svc}-canary -> upstreams."
else
  echo "  (no trace_id given — drive the canary first: drive-canary step 5)"
  echo "  TraceQL: { trace:id = \"<trace_id>\" }"
fi
echo

echo "== 2. Zero canary error/critical log lines (Loki) =="
echo "  LogQL: {service_name=~\"$BACKENDS_RE\"} | json | variant=\"canary\" | level=~\"error|critical\""
echo "  Narrower: {service_name=\"$svc\"} | json | variant=\"canary\" | level=~\"error|critical\""
echo "  PASS: zero lines."
echo

echo "== 3. RUM user-action -> backend trace_id join =="
if [ -n "$testid" ]; then
  echo "  LogQL: {service_name=\"faro\"} | logfmt | event_data_userActionName=\"$testid\""
else
  echo "  LogQL: {service_name=\"faro\"} | logfmt | event_data_userActionName=\"<testid>\""
fi
if [ -n "$trace" ]; then
  echo "  Then confirm the backend saw it: {service_name=~\"$BACKENDS_RE\"} |= \"$trace\""
else
  echo "  Then confirm the backend saw it: {service_name=~\"$BACKENDS_RE\"} |= \"<trace_id>\""
fi
echo "  PASS: the Faro action's trace_id matches the drive-canary traceparent and resolves to ${svc}-canary lines."
echo

echo "== 4. Canary vs stable — success-rate + P99 (PromQL) =="
echo "  # The fm_http_* 'variant' label is being ADDED in a parallel workstream; until those"
echo "  # images redeploy the split may be EMPTY — fall back to the pod label (already present)."
echo "  success-rate by variant:"
echo "    sum by (variant) (rate(fm_http_requests_total{service=\"$svc\",code!~\"5..\"}[10m]))"
echo "      / sum by (variant) (rate(fm_http_requests_total{service=\"$svc\"}[10m]))"
echo "  P99 by variant:"
echo "    histogram_quantile(0.99, sum by (le, variant) (rate(fm_http_request_duration_seconds_bucket{service=\"$svc\"}[10m])))"
echo "  pod-label fallback (until the variant label ships) — canary only:"
echo "    sum(rate(fm_http_requests_total{service=\"$svc\",pod=~\"$svc-canary-.*\",code!~\"5..\"}[10m]))"
echo "      / sum(rate(fm_http_requests_total{service=\"$svc\",pod=~\"$svc-canary-.*\"}[10m]))"
echo "  PASS: canary success-rate >= stable (within noise) and canary P99 not materially worse."
echo

if [ "$routing" = eastwest ]; then
  echo "== EAST-WEST note: '$svc' has NO edge route =="
  echo "  Drive it via a gateway canary whose internal hop lands on ${svc}-canary (for leads: search-canary active),"
  echo "  then verify the propagated marker rode the internal canary:"
  echo "    LogQL: {service_name=\"$svc\"} | json | canary=\"true\""
  echo "  and confirm the SAME trace_id spans the caller-canary -> ${svc}-canary in Tempo (assertion 1)."
  echo
fi

echo "############ end — nothing was executed or changed; run the above via the Grafana MCP ############"
