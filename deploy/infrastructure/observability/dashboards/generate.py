#!/usr/bin/env python3
"""Dashboards-as-code: generate the provisioned Grafana dashboard suite.

    python3 generate.py        # regenerates every *.json next to this file

Edit THIS file, never the emitted JSON — the JSON is build output, checked in
so kustomize's configMapGenerator can pack it into the grafana-dashboards-*
ConfigMaps (see kustomization.yaml here). Grafana is stateless (no PVC): these
provisioned files are the ONLY durable home for dashboards; anything clicked
together in the UI dies with the pod.

Ground rules baked into every builder (verified against the live stack):
  - Datasource uids are the provisioned literals: prometheus / loki / tempo.
  - fm_http_* labels: {service, method, route, status, variant}. `status` is a
    string HTTP code (not `code`); `variant` (stable|canary) only appears on
    series from pods that rolled after the fm_runtime change that added it.
  - Probe routes (/healthz /readyz) are excluded from every RED number.
  - istio_requests_total: always pick ONE reporter per query (destination for
    service-side RED, source for response_flags/edge views) — every request is
    otherwise counted twice.
  - Keycloak's `uri` label holds raw request paths (unbounded cardinality —
    internet scanners land in it): filter on it, NEVER group by it.
  - cAdvisor series carry instance=<k8s node name>; node-exporter series carry
    a `node` label (endpoint node name). Node NETWORK graphs use cAdvisor's
    root cgroup (id="/") because node-exporter runs without hostNetwork here
    (see prometheus.yaml) and its netdev collector would see the pod netns.
  - Colors are a fixed semantic assignment, identical suite-wide:
    stable=blue canary=orange 5xx/error=red 4xx=yellow ok/2xx=green,
    percentile ramps stay in one hue (light->dark blue).
This directory is applied by the infra-dashboards Flux Kustomization WITHOUT
postBuild substitution, so Grafana's ${...} / $var syntax must never pass
through envsubst — do not move these files under a substituted Kustomization.
"""

import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parent

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

# Every RED number excludes probe noise.
NOPROBE = 'route!~"/healthz|/readyz|/metrics"'

# Fixed semantic colors (never cycled, never re-ranked).
C_STABLE = "blue"
C_CANARY = "orange"
C_ERR = "red"
C_WARN = "yellow"
C_OK = "green"
P50, P90, P99 = "super-light-blue", "blue", "dark-blue"


# --------------------------------------------------------------------------- #
# panel builders
# --------------------------------------------------------------------------- #

def t(expr, legend=None, ref="A", instant=False, fmt=None, ds=PROM):
    tgt = {"refId": ref, "expr": expr, "datasource": ds}
    if legend is not None:
        tgt["legendFormat"] = legend
    if instant:
        tgt["instant"] = True
        tgt["range"] = False
    if fmt:
        tgt["format"] = fmt
    return tgt


def lt(expr, legend=None, ref="A", instant=False):
    tgt = {"refId": ref, "expr": expr, "datasource": LOKI}
    if legend is not None:
        tgt["legendFormat"] = legend
    if instant:
        tgt["queryType"] = "instant"
    return tgt


def tt(query, ref="A", limit=20):
    return {
        "refId": ref,
        "datasource": TEMPO,
        "queryType": "traceql",
        "query": query,
        "limit": limit,
        "tableType": "traces",
    }


def steps(*pairs):
    """steps((None,'green'),(1,'yellow'),(5,'red')) -> threshold dict."""
    return {
        "mode": "absolute",
        "steps": [{"value": v, "color": c} for v, c in pairs],
    }


def override_color(name, color, regex=False):
    return {
        "matcher": {"id": "byRegexp" if regex else "byName", "options": name},
        "properties": [
            {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}
        ],
    }


def override_props(name, props, regex=False):
    return {
        "matcher": {"id": "byRegexp" if regex else "byName", "options": name},
        "properties": props,
    }


def timeseries(title, targets, unit="short", desc="", overrides=None, min0=True,
               max_=None, stacked=False, bars=False, fill=10, legend_calcs=None,
               percent=False, decimals=None):
    custom = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 1,
        "fillOpacity": fill,
        "gradientMode": "none",
        "showPoints": "never",
        "pointSize": 4,
        "spanNulls": False,
        "insertNulls": False,
        "barAlignment": 0,
        "axisPlacement": "auto",
        "axisBorderShow": False,
        "scaleDistribution": {"type": "linear"},
        "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
        "thresholdsStyle": {"mode": "off"},
    }
    defaults = {
        "unit": unit,
        "color": {"mode": "palette-classic"},
        "custom": custom,
        "thresholds": steps((None, "green")),
    }
    if min0:
        defaults["min"] = 0
    if max_ is not None:
        defaults["max"] = max_
    if percent:
        defaults["unit"] = "percentunit"
        defaults["max"] = 1
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": targets[0].get("datasource", PROM),
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {
                "displayMode": "table" if legend_calcs else "list",
                "placement": "bottom",
                "showLegend": True,
                "calcs": legend_calcs or [],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title, targets, unit="short", thr=None, desc="", decimals=None,
         color_mode="value", graph_mode="area", text_size=None, mappings=None):
    defaults = {
        "unit": unit,
        "thresholds": thr or steps((None, "blue")),
        "color": {"mode": "thresholds"},
        "mappings": mappings or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    options = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "orientation": "auto",
        "textMode": "auto",
        "colorMode": color_mode,
        "graphMode": graph_mode,
        "justifyMode": "auto",
        "wideLayout": True,
        "percentChangeColorMode": "standard",
        "showPercentChange": False,
    }
    if text_size:
        options["text"] = {"valueSize": text_size}
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": targets[0].get("datasource", PROM),
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": options,
    }


def gauge(title, targets, unit="percentunit", thr=None, desc="", max_=1):
    return {
        "type": "gauge",
        "title": title,
        "description": desc,
        "datasource": targets[0].get("datasource", PROM),
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "max": max_,
                "thresholds": thr or steps((None, C_OK), (0.7, C_WARN), (0.85, C_ERR)),
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "auto",
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
    }


def bargauge(title, targets, unit="percentunit", thr=None, desc="", max_=1):
    return {
        "type": "bargauge",
        "title": title,
        "description": desc,
        "datasource": targets[0].get("datasource", PROM),
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "max": max_,
                "thresholds": thr or steps((None, C_OK), (0.7, C_WARN), (0.85, C_ERR)),
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "displayMode": "basic",
            "showUnfilled": True,
            "valueMode": "color",
            "namePlacement": "auto",
            "sizing": "auto",
        },
    }


def table(title, targets, transformations=None, overrides=None, desc="",
          sort_by=None, defaults=None):
    d = {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                    "inspect": False, "filterable": True},
         "thresholds": steps((None, "green")),
         "color": {"mode": "thresholds"},
         "mappings": []}
    if defaults:
        d.update(defaults)
    return {
        "type": "table",
        "title": title,
        "description": desc,
        "datasource": targets[0].get("datasource", PROM),
        "targets": targets,
        "transformations": transformations or [],
        "fieldConfig": {"defaults": d, "overrides": overrides or []},
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                       "fields": ""},
            "sortBy": sort_by or [],
        },
    }


def heatmap(title, expr, unit="s", desc=""):
    return {
        "type": "heatmap",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "targets": [dict(t(expr, ref="A"), format="heatmap", legendFormat="{{le}}")],
        "fieldConfig": {"defaults": {"custom": {"hideFrom": {
            "tooltip": False, "viz": False, "legend": False}}}, "overrides": []},
        "options": {
            "calculate": False,
            "cellGap": 1,
            "color": {"mode": "scheme", "scheme": "Blues", "steps": 48,
                      "fill": "dark-blue", "reverse": False, "exponent": 0.5},
            "exemplars": {"color": "rgba(255,0,255,0.7)"},
            "filterValues": {"le": 1e-9},
            "legend": {"show": True},
            "rowsFrame": {"layout": "auto"},
            "tooltip": {"mode": "single", "showColorScale": False,
                        "yHistogram": True},
            "yAxis": {"axisPlacement": "left", "reverse": False, "unit": unit},
        },
    }


def logspanel(title, expr, desc=""):
    return {
        "type": "logs",
        "title": title,
        "description": desc,
        "datasource": LOKI,
        "targets": [lt(expr)],
        "options": {
            "showTime": True,
            "showLabels": False,
            "showCommonLabels": False,
            "wrapLogMessage": True,
            "prettifyLogMessage": False,
            "enableLogDetails": True,
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
    }


def traces(title, query, desc="", limit=20):
    return {
        "type": "traces",
        "title": title,
        "description": desc,
        "datasource": TEMPO,
        "targets": [tt(query, limit=limit)],
        "options": {},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def text(md, title=""):
    return {
        "type": "text",
        "title": title,
        "transparent": True,
        "options": {"mode": "markdown", "code": {"language": "plaintext"},
                    "content": md},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


# --------------------------------------------------------------------------- #
# variables
# --------------------------------------------------------------------------- #

def qvar(name, label, query, ds=PROM, multi=False, include_all=False,
         allv=None, regex="", current=None):
    v = {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": ds,
        "query": query,
        "refresh": 2,
        "regex": regex,
        "sort": 1,
        "multi": multi,
        "includeAll": include_all,
        "options": [],
        "current": current or {},
    }
    if allv is not None:
        v["allValue"] = allv
    return v


def loki_label_var(name, label, loki_label, multi=True):
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": LOKI,
        "query": {"refId": "LokiVariableQueryEditor-VariableQuery",
                  "type": 1, "label": loki_label, "stream": ""},
        "refresh": 2,
        "regex": "",
        "sort": 1,
        "multi": multi,
        "includeAll": True,
        "allValue": ".+",
        "options": [],
        "current": {"text": ["All"], "value": ["$__all"], "selected": True},
    }


# --------------------------------------------------------------------------- #
# dashboard assembly
# --------------------------------------------------------------------------- #

class Dash:
    def __init__(self, uid, title, tags, time_from="now-6h", refresh="1m",
                 desc="", variables=None):
        self.uid = uid
        self.title = title
        self.tags = ["funnelmanager"] + tags
        self.time_from = time_from
        self.refresh = refresh
        self.desc = desc
        self.variables = variables or []
        self.panels = []
        self._y = 0
        self._id = 0

    def _nid(self):
        self._id += 1
        return self._id

    def row(self, title, panels_wh):
        """One header + one visual row of (panel, w, h). Widths must sum <=24."""
        self.panels.append({
            "type": "row", "title": title, "collapsed": False, "panels": [],
            "id": self._nid(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y},
        })
        self._y += 1
        self.add(panels_wh)

    def add(self, panels_wh):
        """One visual row of (panel, w, h) laid left to right."""
        x = 0
        maxh = 0
        for panel, w, h in panels_wh:
            panel["id"] = self._nid()
            panel["gridPos"] = {"h": h, "w": w, "x": x, "y": self._y}
            self.panels.append(panel)
            x += w
            maxh = max(maxh, h)
        self._y += maxh

    def build(self):
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.desc,
            "tags": self.tags,
            "editable": True,
            "graphTooltip": 1,
            "schemaVersion": 39,
            "timezone": "browser",
            "fiscalYearStartMonth": 0,
            "weekStart": "",
            "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "timepicker": {},
            "links": [{
                "type": "dashboards",
                "title": "Funnel Manager",
                "tags": ["funnelmanager"],
                "asDropdown": True,
                "includeVars": False,
                "keepTime": True,
                "icon": "external link",
                "targetBlank": False,
                "url": "",
            }],
            "templating": {"list": self.variables},
            "annotations": {"list": [{
                "builtIn": 1,
                "name": "Annotations & Alerts",
                "type": "dashboard",
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "enable": True,
                "hide": True,
                "iconColor": "rgba(0, 211, 255, 1)",
            }]},
            "panels": self.panels,
        }


def write(dash, fname):
    doc = dash.build()
    body = json.dumps(doc, indent=1, sort_keys=False)
    # This dir bypasses Flux postBuild substitution; keep it that way. A "$("
    # or "${" here is fine for Grafana but assert we never emit a bare Flux
    # var-looking token by accident outside Grafana's known macros.
    (OUT / fname).write_text(body + "\n")
    print(f"  {fname}: {len(json.loads(body)['panels'])} panels, {len(body)//1024} KiB")


# =========================================================================== #
# 1. FM · Platform Overview
# =========================================================================== #

def fm_overview():
    d = Dash(
        "fm-overview", "FM · Platform Overview", ["overview"],
        time_from="now-6h", refresh="30s",
        desc="Top-level RED health of every Funnel Manager backend "
             "(fm_http_* series, probe routes excluded). Start here; click a "
             "service row to drill down.",
    )
    d.row("Platform health", [
        (stat("Success rate (5m)",
              [t(f'100 * (sum(rate(fm_http_requests_total{{status!~"5..",{NOPROBE}}}[5m]))'
                 f' / sum(rate(fm_http_requests_total{{{NOPROBE}}}[5m])))')],
              unit="percent", decimals=2,
              thr=steps((None, C_ERR), (99, C_WARN), (99.9, C_OK)),
              desc="Non-5xx share of all backend requests, all services."), 4, 4),
        (stat("Request rate",
              [t(f'sum(rate(fm_http_requests_total{{{NOPROBE}}}[5m]))')],
              unit="reqps", decimals=2, thr=steps((None, "blue"))), 3, 4),
        (stat("P99 latency",
              [t(f'histogram_quantile(0.99, sum by (le) '
                 f'(rate(fm_http_request_duration_seconds_bucket{{{NOPROBE}}}[5m])))')],
              unit="s", thr=steps((None, C_OK), (1, C_WARN), (2.5, C_ERR))), 3, 4),
        (stat("5xx rate",
              [t('sum(rate(fm_http_requests_total{status=~"5.."}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.01, C_WARN), (0.1, C_ERR))), 3, 4),
        (stat("Backend pods running",
              [t('sum(kube_pod_status_phase{namespace="prod", phase="Running"})')],
              thr=steps((None, C_WARN), (10, C_OK)), graph_mode="none",
              desc="Running pods in the prod namespace."), 3, 4),
        (stat("Error logs (15m)",
              [lt('sum(count_over_time({namespace="prod"} | json | '
                  'level=~"error|critical" | __error__="" [15m]))', instant=True)],
              thr=steps((None, C_OK), (1, C_WARN), (20, C_ERR)), graph_mode="none",
              desc="error/critical lines across all prod backends (Loki)."), 3, 4),
        (stat("Nodes ready",
              [t('sum(kube_node_status_condition{condition="Ready", status="true"})')],
              thr=steps((None, C_ERR), (4, C_OK)), graph_mode="none"), 2, 4),
        (stat("CNPG instances up",
              [t('sum(cnpg_collector_up)')],
              thr=steps((None, C_ERR), (5, C_OK)), graph_mode="none"), 3, 4),
    ])
    d.row("RED by service (fm_http_*, probes excluded)", [
        (timeseries("Request rate by service",
                    [t(f'sum by (service) (rate(fm_http_requests_total{{{NOPROBE}}}[$__rate_interval]))',
                       "{{service}}")], unit="reqps"), 8, 8),
        (timeseries("5xx rate by service",
                    [t('sum by (service) (rate(fm_http_requests_total{status=~"5.."}[$__rate_interval]))',
                       "{{service}}")], unit="reqps",
                    desc="Empty = no server errors in the window."), 8, 8),
        (timeseries("P99 latency by service",
                    [t(f'histogram_quantile(0.99, sum by (le, service) '
                       f'(rate(fm_http_request_duration_seconds_bucket{{{NOPROBE}}}[$__rate_interval])))',
                       "{{service}}")], unit="s"), 8, 8),
    ])
    svc_table = table(
        "Services — current RED (click a service to drill down)",
        [
            t(f'sum by (service) (rate(fm_http_requests_total{{{NOPROBE}}}[5m]))',
              ref="A", instant=True, fmt="table"),
            t(f'100 * (sum by (service) (rate(fm_http_requests_total{{status!~"5..",{NOPROBE}}}[5m]))'
              f' / sum by (service) (rate(fm_http_requests_total{{{NOPROBE}}}[5m])))',
              ref="B", instant=True, fmt="table"),
            t(f'histogram_quantile(0.99, sum by (le, service) '
              f'(rate(fm_http_request_duration_seconds_bucket{{{NOPROBE}}}[5m])))',
              ref="C", instant=True, fmt="table"),
            t('count by (service) (count by (service, pod) (fm_http_requests_total))',
              ref="D", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "service", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True,
                                  "Time 3": True, "Time 4": True},
                "renameByName": {"Value #A": "req/s", "Value #B": "success %",
                                 "Value #C": "p99 (s)", "Value #D": "pods"},
            }},
        ],
        overrides=[
            override_props("req/s", [{"id": "unit", "value": "reqps"},
                                     {"id": "decimals", "value": 2}]),
            override_props("success %", [
                {"id": "unit", "value": "percent"},
                {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))},
            ]),
            override_props("p99 (s)", [{"id": "unit", "value": "s"}]),
            override_props("service", [{"id": "links", "value": [{
                "title": "Drill down: ${__value.text}",
                "url": "/d/fm-service/fm-service-drilldown?var-service=${__value.text}&${__url_time_range}",
            }]}]),
        ],
        sort_by=[{"displayName": "req/s", "desc": True}],
    )
    status_class = timeseries(
        "Requests by status class",
        [t('sum by (class) (label_replace(rate(fm_http_requests_total{'
           + NOPROBE + '}[$__rate_interval]), "class", "${1}xx", "status", "([0-9]).."))',
           "{{class}}")],
        unit="reqps", bars=True, stacked=True, fill=60,
        overrides=[override_color("2xx", C_OK), override_color("3xx", "purple"),
                   override_color("4xx", C_WARN), override_color("5xx", C_ERR)])
    d.row("Services", [(svc_table, 14, 9), (status_class, 10, 9)])
    d.row("Recent errors (all backends)", [
        (logspanel("error/critical log lines — prod namespace",
                   '{namespace="prod"} | json | level=~"error|critical" | __error__=""',
                   desc="Log line TraceID links jump straight to the Tempo trace."),
         24, 10),
    ])
    return d


# =========================================================================== #
# 2. FM · Service Drilldown
# =========================================================================== #

def fm_service():
    sel = f'service=~"$service", {NOPROBE}, route=~"$route"'
    d = Dash(
        "fm-service", "FM · Service Drilldown", ["service"],
        time_from="now-6h", refresh="1m",
        desc="Single-service RED + per-route breakdown + pod resources + logs. "
             "Linked from the Platform Overview services table.",
        variables=[
            qvar("service", "service",
                 "label_values(fm_http_requests_total, service)",
                 current={"text": "search", "value": "search", "selected": True}),
            qvar("route", "route",
                 'label_values(fm_http_requests_total{service=~"$service"}, route)',
                 multi=True, include_all=True, allv=".*",
                 current={"text": ["All"], "value": ["$__all"], "selected": True}),
        ],
    )
    d.row("$service — health", [
        (stat("Request rate", [t(f'sum(rate(fm_http_requests_total{{{sel}}}[5m]))')],
              unit="reqps", decimals=2), 4, 4),
        (stat("Success rate",
              [t(f'100 * (sum(rate(fm_http_requests_total{{status!~"5..",{sel}}}[5m]))'
                 f' / sum(rate(fm_http_requests_total{{{sel}}}[5m])))')],
              unit="percent", decimals=2,
              thr=steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))), 4, 4),
        (stat("P99", [t(f'histogram_quantile(0.99, sum by (le) '
                        f'(rate(fm_http_request_duration_seconds_bucket{{service=~"$service",{NOPROBE}}}[5m])))')],
              unit="s", thr=steps((None, C_OK), (1, C_WARN), (2.5, C_ERR))), 4, 4),
        (stat("5xx (1h)", [t(f'sum(increase(fm_http_requests_total{{status=~"5..",service=~"$service"}}[1h])) or vector(0)')],
              thr=steps((None, C_OK), (1, C_WARN), (10, C_ERR)), decimals=0), 4, 4),
        (stat("Pods running",
              [t('sum(kube_pod_status_phase{namespace="prod", phase="Running", pod=~"$service-.*"})')],
              thr=steps((None, C_ERR), (1, C_OK)), graph_mode="none",
              desc="Counts stable + canary pods of this service."), 4, 4),
        (stat("Error logs (1h)",
              [lt('sum(count_over_time({service_name="$service"} | json | '
                  'level=~"error|critical" | __error__="" [1h]))', instant=True)],
              thr=steps((None, C_OK), (1, C_WARN), (20, C_ERR)),
              graph_mode="none"), 4, 4),
    ])
    d.row("RED", [
        (timeseries("Request rate by route",
                    [t(f'sum by (route) (rate(fm_http_requests_total{{{sel}}}[$__rate_interval]))',
                       "{{method}} {{route}}")], unit="reqps"), 12, 8),
        (timeseries("Errors by status",
                    [t(f'sum by (status) (rate(fm_http_requests_total{{status=~"4..|5..",service=~"$service",route=~"$route"}}[$__rate_interval]))',
                       "{{status}}")], unit="reqps",
                    overrides=[override_color("4..", C_WARN, regex=True),
                               override_color("5..", C_ERR, regex=True)]), 12, 8),
    ])
    d.row("Latency", [
        (timeseries("Percentiles",
                    [t(f'histogram_quantile(0.5, sum by (le) (rate(fm_http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))', "p50", ref="A"),
                     t(f'histogram_quantile(0.9, sum by (le) (rate(fm_http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))', "p90", ref="B"),
                     t(f'histogram_quantile(0.99, sum by (le) (rate(fm_http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval])))', "p99", ref="C")],
                    unit="s",
                    overrides=[override_color("p50", P50),
                               override_color("p90", P90),
                               override_color("p99", P99)],
                    desc="Percentile lines answer 'how bad'; the heatmap beside "
                         "shows the full distribution (bimodality, drift)."), 12, 8),
        (heatmap("Latency distribution",
                 f'sum by (le) (increase(fm_http_request_duration_seconds_bucket{{{sel}}}[$__rate_interval]))'),
         12, 8),
    ])
    route_table = table(
        "Routes — current RED",
        [
            t(f'sum by (route, method) (rate(fm_http_requests_total{{{sel}}}[5m]))',
              ref="A", instant=True, fmt="table"),
            t(f'100 * (sum by (route, method) (rate(fm_http_requests_total{{status!~"5..",{sel}}}[5m]))'
              f' / sum by (route, method) (rate(fm_http_requests_total{{{sel}}}[5m])))',
              ref="B", instant=True, fmt="table"),
            t(f'histogram_quantile(0.99, sum by (le, route, method) '
              f'(rate(fm_http_request_duration_seconds_bucket{{{sel}}}[5m])))',
              ref="C", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "route", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True,
                                  "Time 3": True, "method 2": True, "method 3": True},
                "renameByName": {"Value #A": "req/s", "Value #B": "success %",
                                 "Value #C": "p99 (s)", "method 1": "method"},
            }},
        ],
        overrides=[
            override_props("req/s", [{"id": "unit", "value": "reqps"},
                                     {"id": "decimals", "value": 3}]),
            override_props("success %", [
                {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))},
            ]),
            override_props("p99 (s)", [{"id": "unit", "value": "s"}]),
        ],
        sort_by=[{"displayName": "req/s", "desc": True}],
    )
    d.row("Routes", [(route_table, 24, 9)])
    d.row("Pod resources", [
        (timeseries("CPU by pod",
                    [t('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="prod", pod=~"$service-.*", container!="", image!=""}[$__rate_interval]))',
                       "{{pod}}")], unit="cores", decimals=3), 8, 8),
        (timeseries("Memory (working set) by pod",
                    [t('sum by (pod) (container_memory_working_set_bytes{namespace="prod", pod=~"$service-.*", container!="", image!=""})',
                       "{{pod}}")], unit="bytes"), 8, 8),
        (timeseries("Container restarts (1h window)",
                    [t('sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="prod", pod=~"$service-.*"}[1h]))',
                       "{{pod}}")], decimals=0,
                    desc="Anything above zero deserves a look at the logs below."),
         8, 8),
    ])
    d.row("Logs", [
        (timeseries("Log lines by level",
                    [lt('sum by (level) (count_over_time({service_name="$service"} | json | level != "" | __error__="" [$__auto]))',
                        "{{level}}")],
                    unit="short", bars=True, stacked=True, fill=60,
                    overrides=[override_color("info", "blue"),
                               override_color("debug", "text"),
                               override_color("warning", C_WARN),
                               override_color("error", C_ERR),
                               override_color("critical", "dark-red")]), 8, 10),
        (logspanel("warning/error/critical — $service",
                   '{service_name="$service"} | json | level=~"warning|error|critical" | __error__=""'),
         16, 10),
    ])
    return d


# =========================================================================== #
# 3. FM · Canary vs Stable
# =========================================================================== #

def fm_canary():
    d = Dash(
        "fm-canary", "FM · Canary vs Stable", ["canary"],
        time_from="now-3h", refresh="30s",
        desc="Promotion-gate view: canary vs stable success rate, latency and "
             "errors. Istio panels split by workload and work immediately; "
             "fm_http panels split by the `variant` label, which only appears "
             "on series from pods that rolled with the fm_runtime change that "
             "added it — until the fleet rolls, trust the Istio + Loki panels.",
        variables=[
            qvar("svc", "canary service",
                 'label_values(istio_requests_total{destination_workload=~".+-canary"}, destination_workload)',
                 regex="/^(?<value>.*)-canary$/",
                 current={"text": "search", "value": "search", "selected": True}),
        ],
    )
    d.row("Gate — $svc canary vs stable (Istio, works today)", [
        (stat("Canary pods",
              [t('count(kube_pod_info{namespace="prod", pod=~".*-canary-.*"}) or vector(0)')],
              thr=steps((None, "text"), (1, C_CANARY)), graph_mode="none",
              desc="All canary pods currently scheduled (any service). "
                   "0 = no active canary; requests fall back to stable."), 3, 4),
        (stat("Canary success rate",
              [t('100 * (sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary", response_code!~"5.."}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary"}[5m])))')],
              unit="percent", decimals=2,
              thr=steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))), 5, 4),
        (stat("Stable success rate",
              [t('100 * (sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc", response_code!~"5.."}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc"}[5m])))')],
              unit="percent", decimals=2,
              thr=steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))), 5, 4),
        (stat("Success delta (canary − stable)",
              [t('(sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary", response_code!~"5.."}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary"}[5m])))'
                 ' - (sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc", response_code!~"5.."}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc"}[5m])))')],
              unit="percentunit", decimals=4,
              thr=steps((None, C_ERR), (-0.005, C_WARN), (-0.0001, C_OK)),
              desc="Red = the canary fails more than stable. The promotion "
                   "gate wants this at ~0."), 6, 4),
        (stat("P99 delta (canary − stable)",
              [t('(histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket{reporter="destination", destination_workload="$svc-canary"}[5m])))'
                 ' - histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket{reporter="destination", destination_workload="$svc"}[5m]))))')],
              unit="ms", decimals=1,
              thr=steps((None, C_OK), (50, C_WARN), (250, C_ERR)),
              desc="Positive = canary is slower than stable at p99."), 5, 4),
    ])
    canary_stable = [override_color("canary", C_CANARY),
                     override_color("stable", C_STABLE)]
    d.row("Istio side-by-side", [
        (timeseries("Request rate",
                    [t('sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary"}[$__rate_interval]))', "canary", ref="A"),
                     t('sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc"}[$__rate_interval]))', "stable", ref="B")],
                    unit="reqps", overrides=canary_stable), 8, 8),
        (timeseries("Success rate",
                    [t('sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary", response_code!~"5.."}[$__rate_interval])) / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc-canary"}[$__rate_interval]))', "canary", ref="A"),
                     t('sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc", response_code!~"5.."}[$__rate_interval])) / sum(rate(istio_requests_total{reporter="destination", destination_workload="$svc"}[$__rate_interval]))', "stable", ref="B")],
                    percent=True, overrides=canary_stable), 8, 8),
        (timeseries("P99 latency",
                    [t('histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket{reporter="destination", destination_workload="$svc-canary"}[$__rate_interval])))', "canary", ref="A"),
                     t('histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket{reporter="destination", destination_workload="$svc"}[$__rate_interval])))', "stable", ref="B")],
                    unit="ms", overrides=canary_stable), 8, 8),
    ])
    d.row("fm_http variant split (populates as the fleet rolls)", [
        (timeseries("Success rate by variant",
                    [t('sum by (variant) (rate(fm_http_requests_total{variant!="", status!~"5.."}[$__rate_interval])) / sum by (variant) (rate(fm_http_requests_total{variant!=""}[$__rate_interval]))',
                       "{{variant}}")], percent=True, overrides=canary_stable), 8, 8),
        (timeseries("P99 by variant",
                    [t('histogram_quantile(0.99, sum by (le, variant) (rate(fm_http_request_duration_seconds_bucket{variant!=""}[$__rate_interval])))',
                       "{{variant}}")], unit="s", overrides=canary_stable), 8, 8),
        (timeseries("Request rate by variant",
                    [t('sum by (variant) (rate(fm_http_requests_total{variant!=""}[$__rate_interval]))',
                       "{{variant}}")], unit="reqps", overrides=canary_stable), 8, 8),
    ])
    d.row("Logs & browser (variant is stamped on every log line today)", [
        (timeseries("error/critical log rate by variant",
                    [lt('sum by (variant) (count_over_time({namespace="prod"} | json | level=~"error|critical" | variant != "" | __error__="" [$__auto]))',
                        "{{variant}}")],
                    bars=True, stacked=True, fill=60, overrides=canary_stable), 8, 10),
        (timeseries("Canary browser (Faro): events & exceptions",
                    [lt('sum(count_over_time({job="faro"} | logfmt | app_version=~"canary-.*" | kind="event" [$__auto]))', "events", ref="A"),
                     lt('sum(count_over_time({job="faro"} | logfmt | app_version=~"canary-.*" | kind="exception" [$__auto]))', "exceptions", ref="B")],
                    overrides=[override_color("events", C_CANARY),
                               override_color("exceptions", C_ERR)],
                    desc="Filtered to app_version=canary-* bundles."), 8, 10),
        (logspanel("Canary error logs",
                   '{namespace="prod"} | json | variant="canary" | level=~"warning|error|critical" | __error__=""'),
         8, 10),
    ])
    return d


# =========================================================================== #
# 4. FM · Logs & Errors
# =========================================================================== #

def fm_logs():
    d = Dash(
        "fm-logs", "FM · Logs & Errors", ["logs"],
        time_from="now-3h", refresh="1m",
        desc="Loki view across every service. Backend lines are fm_runtime "
             "structured JSON ({service_name=...} | json); browser RUM lands "
             "under {job=\"faro\"}. Log-line TraceIDs deep-link to Tempo.",
        variables=[loki_label_var("logsvc", "service", "service_name")],
    )
    d.row("Volume", [
        (timeseries("Log lines by service",
                    [lt('sum by (service_name) (count_over_time({job="fluent-bit", service_name=~"$logsvc"} [$__auto]))',
                        "{{service_name}}")], bars=True, stacked=True, fill=60), 12, 8),
        (timeseries("Log throughput (bytes)",
                    [lt('sum by (service_name) (bytes_over_time({job="fluent-bit", service_name=~"$logsvc"} [$__auto]))',
                        "{{service_name}}")], unit="bytes", bars=True,
                    stacked=True, fill=60,
                    desc="Loki bills by bytes scanned — this is who is noisy."),
         12, 8),
    ])
    d.row("Errors", [
        (timeseries("error-level lines by service (all namespaces)",
                    [lt('sum by (service_name) (count_over_time({job="fluent-bit", service_name=~"$logsvc"} | detected_level=~"error|critical" [$__auto]))',
                        "{{service_name}}")],
                    desc="Uses Loki's detected_level (works for every log "
                         "format, incl. non-JSON infra logs)."), 8, 8),
        (timeseries("prod backends by level (fm_runtime JSON)",
                    [lt('sum by (level) (count_over_time({namespace="prod"} | json | level != "" | __error__="" [$__auto]))',
                        "{{level}}")],
                    bars=True, stacked=True, fill=60,
                    overrides=[override_color("info", "blue"),
                               override_color("debug", "text"),
                               override_color("warning", C_WARN),
                               override_color("error", C_ERR),
                               override_color("critical", "dark-red")]), 8, 8),
        (table("Noisiest pods (window total)",
               [lt('topk(15, sum by (pod) (count_over_time({job="fluent-bit"} [$__range])))',
                   instant=True)],
               transformations=[
                   {"id": "organize", "options": {
                       "excludeByName": {"Time": True},
                       "renameByName": {"Value": "lines"}}},
               ],
               sort_by=[{"displayName": "lines", "desc": True}]), 8, 8),
    ])
    d.row("Browser (Faro RUM)", [
        (timeseries("Faro entries by kind",
                    [lt('sum by (kind) (count_over_time({job="faro"} | logfmt | kind != "" [$__auto]))',
                        "{{kind}}")],
                    bars=True, stacked=True, fill=60,
                    overrides=[override_color("exception", C_ERR)]), 12, 8),
        (logspanel("Browser exceptions",
                   '{job="faro"} | logfmt | kind="exception"'), 12, 8),
    ])
    d.row("Tail", [
        (logspanel("Errors — everything",
                   '{job="fluent-bit", service_name=~"$logsvc"} | detected_level=~"error|critical"'),
         12, 12),
        (logspanel("Raw tail — selected services",
                   '{job="fluent-bit", service_name=~"$logsvc"}'), 12, 12),
    ])
    return d


# =========================================================================== #
# 5. FM · Traces
# =========================================================================== #

def fm_traces():
    d = Dash(
        "fm-traces", "FM · Traces (Tempo)", ["traces"],
        time_from="now-1h", refresh="",
        desc="TraceQL search panels over the mesh spans. Baseline sampling is "
             "5% of unmarked prod traffic; canary flows are fully traced "
             "(Faro originates sampled=1 and every sidecar honors it).",
    )
    d.row("How to use this", [(text(
        "**Pivots beat searching.** A backend log line's `TraceID` derived "
        "field opens the trace; any span links back to its Loki lines "
        "(trace→logs). `http.url` is deliberately redacted mesh-wide "
        "(secret-bearing paths) — identify requests by route/span name.\n\n"
        "- **Sampling:** ~5% baseline on unmarked prod traffic; canary "
        "requests are always fully traced end-to-end.\n"
        "- Span service names look like `search.prod`, `search-canary.prod`, "
        "`istio-ingress.istio-ingress`."), 24, 3)])
    d.row("Slow & broken", [
        (traces("Slowest traces (>1s)", "{duration > 1s}",
                desc="Any span over a second, newest first."), 12, 10),
        (traces("Error traces", "{status = error}",
                desc="Spans Envoy marked as errored (5xx & resets)."), 12, 10),
    ])
    d.row("Canary & ingress", [
        (traces("Canary traces", '{resource.service.name =~ ".*-canary.*"}',
                desc="Every span that touched a canary workload — these are "
                     "always sampled."), 12, 10),
        (traces("Ingress roots",
                '{resource.service.name = "istio-ingress.istio-ingress" && kind = server}',
                desc="Traces as they entered the mesh at the gateway."), 12, 10),
    ])
    return d


# =========================================================================== #
# 6. FM · Browser RUM (Faro)
# =========================================================================== #

def fm_rum():
    # `by ()` collapses the per-stream quantiles into one cross-stream value.
    vital = ('quantile_over_time(0.75, {job="faro"} | logfmt | '
             'kind="measurement" | unwrap value_%s | __error__="" [$__range]) by ()')
    d = Dash(
        "fm-rum", "FM · Browser RUM (Faro)", ["rum", "frontend"],
        time_from="now-24h", refresh="5m",
        desc="Grafana Faro browser telemetry from telemetry-enabled SPA "
             "bundles (dev, canary, and any bundle built with VITE_TELEMETRY). "
             "Web vitals at p75 (the industry aggregation), user actions by "
             "data-testid, JS exceptions.",
    )
    d.row("Core Web Vitals — p75 over the dashboard window", [
        (stat("LCP", [lt(vital % "lcp", instant=True)], unit="ms",
              thr=steps((None, C_OK), (2500, C_WARN), (4000, C_ERR)),
              desc="Largest Contentful Paint. good <2.5s, poor >4s."), 5, 5),
        (stat("INP", [lt(vital % "inp", instant=True)], unit="ms",
              thr=steps((None, C_OK), (200, C_WARN), (500, C_ERR)),
              desc="Interaction to Next Paint. good <200ms, poor >500ms. "
                   "Needs real interactions to emit."), 5, 5),
        (stat("CLS", [lt(vital % "cls", instant=True)], decimals=3,
              thr=steps((None, C_OK), (0.1, C_WARN), (0.25, C_ERR)),
              desc="Cumulative Layout Shift. good <0.1, poor >0.25."), 4, 5),
        (stat("FCP", [lt(vital % "fcp", instant=True)], unit="ms",
              thr=steps((None, C_OK), (1800, C_WARN), (3000, C_ERR)),
              desc="First Contentful Paint. good <1.8s, poor >3s."), 5, 5),
        (stat("TTFB", [lt(vital % "time_to_first_byte", instant=True)], unit="ms",
              thr=steps((None, C_OK), (800, C_WARN), (1800, C_ERR)),
              desc="Time To First Byte. good <0.8s, poor >1.8s."), 5, 5),
    ])
    d.row("Over time", [
        (timeseries("LCP p75 by app",
                    [lt('quantile_over_time(0.75, {job="faro"} | logfmt | kind="measurement" | unwrap value_lcp | __error__="" [$__auto]) by (app_name)',
                        "{{app_name}}")], unit="ms"), 8, 8),
        (timeseries("TTFB p75 by app",
                    [lt('quantile_over_time(0.75, {job="faro"} | logfmt | kind="measurement" | unwrap value_time_to_first_byte | __error__="" [$__auto]) by (app_name)',
                        "{{app_name}}")], unit="ms"), 8, 8),
        (timeseries("JS exceptions",
                    [lt('sum by (app_name) (count_over_time({job="faro"} | logfmt | kind="exception" [$__auto]))',
                        "{{app_name}}")], bars=True, stacked=True, fill=60,
                    overrides=[]), 8, 8),
    ])
    d.row("Sessions & actions", [
        (timeseries("Active sessions",
                    [lt('count(sum by (session_id) (count_over_time({job="faro"} [$__auto])))',
                        "sessions")],
                    decimals=0, overrides=[override_color("sessions", "purple")]),
         8, 9),
        (table("User actions by data-testid (window total)",
               [lt('topk(20, sum by (event_data_userActionName) (count_over_time({job="faro"} | logfmt | kind="event" | event_data_userActionName != "" [$__range])))',
                   instant=True)],
               transformations=[
                   {"id": "organize", "options": {
                       "excludeByName": {"Time": True},
                       "renameByName": {"event_data_userActionName": "testid",
                                        "Value": "count"}}},
               ],
               sort_by=[{"displayName": "count", "desc": True}],
               desc="The data-testid is both the Faro user-action name and "
                    "the Playwright selector (drive-canary)."), 8, 9),
        (table("Events by name (window total)",
               [lt('topk(20, sum by (event_name) (count_over_time({job="faro"} | logfmt | kind="event" [$__range])))',
                   instant=True)],
               transformations=[
                   {"id": "organize", "options": {
                       "excludeByName": {"Time": True},
                       "renameByName": {"event_name": "event",
                                        "Value": "count"}}},
               ],
               sort_by=[{"displayName": "count", "desc": True}]), 8, 9),
    ])
    d.row("Recent browser errors", [
        (logspanel("Exceptions — full detail",
                   '{job="faro"} | logfmt | kind="exception"'), 24, 10),
    ])
    return d


# =========================================================================== #
# 7. Kubernetes · Cluster & Nodes
# =========================================================================== #

def k8s_cluster():
    d = Dash(
        "k8s-cluster", "Kubernetes · Cluster & Nodes", ["kubernetes"],
        time_from="now-3h", refresh="1m",
        desc="USE view of the k3s cluster: node CPU/memory/disk/network, "
             "commitment vs allocatable, pod placement per node, PVC fill. "
             "Node network graphs come from cAdvisor's root cgroup (host-"
             "accurate); the rest is node-exporter.",
    )
    d.row("Cluster", [
        (stat("Nodes ready",
              [t('sum(kube_node_status_condition{condition="Ready", status="true"})')],
              thr=steps((None, C_ERR), (4, C_OK)), graph_mode="none"), 3, 5),
        (gauge("CPU utilisation",
               [t('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))')],
               desc="Whole-cluster CPU busy share (node-exporter)."), 4, 5),
        (gauge("Memory utilisation",
               [t('1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes)')]),
         4, 5),
        (gauge("CPU requests commitment",
               [t('sum(kube_pod_container_resource_requests{resource="cpu"}) / sum(kube_node_status_allocatable{resource="cpu"})')],
               thr=steps((None, C_OK), (0.8, C_WARN), (1, C_ERR)),
               desc="Scheduled CPU requests vs allocatable. >100% = "
                    "overcommitted requests."), 4, 5),
        (gauge("Memory requests commitment",
               [t('sum(kube_pod_container_resource_requests{resource="memory"}) / sum(kube_node_status_allocatable{resource="memory"})')],
               thr=steps((None, C_OK), (0.8, C_WARN), (1, C_ERR))), 4, 5),
        (stat("Pods running", [t('sum(kube_pod_status_phase{phase="Running"})')],
              thr=steps((None, "blue")), graph_mode="none"), 2, 5),
        (stat("Pods not running",
              [t('sum(kube_pod_status_phase{phase=~"Pending|Failed|Unknown"})')],
              thr=steps((None, C_OK), (1, C_WARN), (3, C_ERR)),
              graph_mode="none"), 3, 5),
    ])
    d.row("Node USE", [
        (timeseries("CPU busy % by node",
                    [t('100 * (1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval])))',
                       "{{node}}")], unit="percent", max_=100), 8, 8),
        (timeseries("Memory used % by node",
                    [t('100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
                       "{{node}}")], unit="percent", max_=100), 8, 8),
        (timeseries("Load (5m) per core by node",
                    [t('node_load5 / on(node) count by (node) (node_cpu_seconds_total{mode="idle"})',
                       "{{node}}")],
                    desc=">1 means runnable tasks exceed cores (saturation)."),
         8, 8),
    ])
    d.row("Disk & network", [
        (timeseries("Root filesystem used % by node",
                    [t('100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
                       "{{node}}")], unit="percent", max_=100), 8, 8),
        (timeseries("Disk I/O utilisation by node",
                    [t('sum by (node) (rate(node_disk_io_time_seconds_total[$__rate_interval]))',
                       "{{node}}")], unit="percentunit",
                    desc="Share of wall-clock the disks were busy."), 8, 8),
        (timeseries("Node network (rx +, tx −)",
                    [t('sum by (instance) (rate(container_network_receive_bytes_total{id="/"}[$__rate_interval]))',
                       "rx {{instance}}", ref="A"),
                     t('sum by (instance) (rate(container_network_transmit_bytes_total{id="/"}[$__rate_interval]))',
                       "tx {{instance}}", ref="B")],
                    unit="Bps", min0=False,
                    overrides=[override_props("/^tx .*/", [
                        {"id": "custom.transform", "value": "negative-Y"}],
                        regex=True)],
                    desc="cAdvisor root cgroup (host NICs; node-exporter here "
                         "sees only its pod netns)."), 8, 8),
    ])
    placement = table(
        "Pod placement — who runs where",
        [
            t('kube_pod_info', ref="A", instant=True, fmt="table"),
            t('sum by (namespace, pod) (kube_pod_container_status_restarts_total)',
              ref="B", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "pod", "mode": "outer"}},
            {"id": "filterFieldsByName", "options": {"include": {
                "names": ["namespace 1", "pod", "node", "created_by_kind",
                          "Value #B"]}}},
            {"id": "organize", "options": {
                "renameByName": {"namespace 1": "namespace",
                                 "created_by_kind": "kind",
                                 "Value #B": "restarts"}}},
        ],
        overrides=[
            override_props("restarts", [
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, "transparent"), (1, C_WARN), (5, C_ERR))},
            ]),
        ],
        sort_by=[{"displayName": "node", "desc": False}],
        desc="Every pod with its node. Sort/filter by any column.",
    )
    d.row("Placement & pressure", [
        (placement, 12, 12),
        (timeseries("Pods per node",
                    [t('count by (node) (kube_pod_info)', "{{node}}")],
                    decimals=0), 6, 12),
        (bargauge("PVC fill",
                  [t('100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes',
                     "{{namespace}}/{{persistentvolumeclaim}}", instant=True)],
                  unit="percent", max_=100,
                  thr=steps((None, C_OK), (70, C_WARN), (85, C_ERR))), 6, 12),
    ])
    nodes_info = table(
        "Nodes",
        [
            t('kube_node_info', ref="A", instant=True, fmt="table"),
            t('kube_node_status_allocatable{resource="cpu"}', ref="B",
              instant=True, fmt="table"),
            t('kube_node_status_allocatable{resource="memory"}', ref="C",
              instant=True, fmt="table"),
            t('sum by (node) (increase(kube_pod_container_status_restarts_total[24h]))',
              ref="D", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "node", "mode": "outer"}},
            {"id": "filterFieldsByName", "options": {"include": {
                "names": ["node", "internal_ip", "kubelet_version", "os_image",
                          "Value #B", "Value #C", "Value #D"]}}},
            {"id": "organize", "options": {
                "renameByName": {"internal_ip": "ip",
                                 "kubelet_version": "kubelet",
                                 "os_image": "os",
                                 "Value #B": "cpu alloc",
                                 "Value #C": "mem alloc",
                                 "Value #D": "restarts 24h"}}},
        ],
        overrides=[
            override_props("mem alloc", [{"id": "unit", "value": "bytes"}]),
            override_props("cpu alloc", [{"id": "unit", "value": "cores"}]),
        ],
        sort_by=[{"displayName": "node", "desc": False}],
    )
    d.row("Inventory", [
        (nodes_info, 14, 8),
        (timeseries("Container restarts cluster-wide (1h window)",
                    [t('sum by (namespace) (increase(kube_pod_container_status_restarts_total[1h]))',
                       "{{namespace}}")], decimals=0), 10, 8),
    ])
    return d


# =========================================================================== #
# 8. Kubernetes · Pods & Resources
# =========================================================================== #

def k8s_pods():
    csel = 'namespace=~"$namespace", pod=~"$pod", container!="", image!=""'
    d = Dash(
        "k8s-pods", "Kubernetes · Pods & Resources", ["kubernetes", "pods"],
        time_from="now-3h", refresh="1m",
        desc="Per-pod CPU/memory vs requests & limits (kubernetes-mixin "
             "shape), throttling, network, restarts, OOM kills.",
        variables=[
            qvar("namespace", "namespace",
                 "label_values(kube_pod_info, namespace)", multi=True,
                 include_all=True, allv=".*",
                 current={"text": ["prod"], "value": ["prod"], "selected": True}),
            qvar("pod", "pod",
                 'label_values(kube_pod_info{namespace=~"$namespace"}, pod)',
                 multi=True, include_all=True, allv=".*",
                 current={"text": ["All"], "value": ["$__all"], "selected": True}),
        ],
    )
    d.row("Health", [
        (stat("Pods running",
              [t('sum(kube_pod_status_phase{namespace=~"$namespace", phase="Running"})')],
              thr=steps((None, "blue")), graph_mode="none"), 4, 4),
        (stat("Restarts (1h)",
              [t('sum(increase(kube_pod_container_status_restarts_total{namespace=~"$namespace", pod=~"$pod"}[1h])) or vector(0)')],
              decimals=0, thr=steps((None, C_OK), (1, C_WARN), (5, C_ERR)),
              graph_mode="none"), 4, 4),
        (stat("Containers whose last exit was OOMKilled",
              [t('sum(kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace=~"$namespace", pod=~"$pod"}) or vector(0)')],
              decimals=0, thr=steps((None, C_OK), (1, C_ERR)),
              graph_mode="none"), 5, 4),
        (stat("CPU used / requested",
              [t('sum(rate(container_cpu_usage_seconds_total{' + csel + '}[5m])) / sum(kube_pod_container_resource_requests{resource="cpu", namespace=~"$namespace", pod=~"$pod"})')],
              unit="percentunit", decimals=1,
              thr=steps((None, C_OK), (0.9, C_WARN), (1.2, C_ERR))), 5, 4),
        (stat("Memory used / requested",
              [t('sum(container_memory_working_set_bytes{' + csel + '}) / sum(kube_pod_container_resource_requests{resource="memory", namespace=~"$namespace", pod=~"$pod"})')],
              unit="percentunit", decimals=1,
              thr=steps((None, C_OK), (0.9, C_WARN), (1.2, C_ERR))), 6, 4),
    ])
    d.row("CPU", [
        (timeseries("CPU usage by pod",
                    [t('sum by (pod) (rate(container_cpu_usage_seconds_total{' + csel + '}[$__rate_interval]))',
                       "{{pod}}")], unit="cores", decimals=3), 12, 8),
        (timeseries("CPU throttling % by pod",
                    [t('100 * sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace=~"$namespace", pod=~"$pod", container!=""}[$__rate_interval]))'
                       ' / sum by (pod) (rate(container_cpu_cfs_periods_total{namespace=~"$namespace", pod=~"$pod", container!=""}[$__rate_interval]))',
                       "{{pod}}")], unit="percent", max_=100,
                    desc="Sustained throttling = the CPU limit is too low for "
                         "this workload."), 12, 8),
    ])
    cpu_table = table(
        "CPU quota — usage vs requests & limits",
        [
            t('sum by (pod) (rate(container_cpu_usage_seconds_total{' + csel + '}[5m]))',
              ref="A", instant=True, fmt="table"),
            t('sum by (pod) (kube_pod_container_resource_requests{resource="cpu", namespace=~"$namespace", pod=~"$pod"})',
              ref="B", instant=True, fmt="table"),
            t('sum by (pod) (rate(container_cpu_usage_seconds_total{' + csel + '}[5m]))'
              ' / sum by (pod) (kube_pod_container_resource_requests{resource="cpu", namespace=~"$namespace", pod=~"$pod"})',
              ref="C", instant=True, fmt="table"),
            t('sum by (pod) (kube_pod_container_resource_limits{resource="cpu", namespace=~"$namespace", pod=~"$pod"})',
              ref="D", instant=True, fmt="table"),
            t('sum by (pod) (rate(container_cpu_usage_seconds_total{' + csel + '}[5m]))'
              ' / sum by (pod) (kube_pod_container_resource_limits{resource="cpu", namespace=~"$namespace", pod=~"$pod"})',
              ref="E", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "pod", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True,
                                  "Time 3": True, "Time 4": True, "Time 5": True},
                "renameByName": {"Value #A": "usage (cores)",
                                 "Value #B": "requests",
                                 "Value #C": "usage/requests",
                                 "Value #D": "limits",
                                 "Value #E": "usage/limits"}}},
        ],
        overrides=[
            override_props("usage (cores)", [{"id": "decimals", "value": 3}]),
            override_props("requests", [{"id": "decimals", "value": 2}]),
            override_props("limits", [{"id": "decimals", "value": 2}]),
            override_props("/^usage\\//", [
                {"id": "unit", "value": "percentunit"},
                {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, C_OK), (0.9, C_WARN), (1.1, C_ERR))},
            ], regex=True),
        ],
        sort_by=[{"displayName": "usage (cores)", "desc": True}],
    )
    mem_table = table(
        "Memory quota — working set vs requests & limits",
        [
            t('sum by (pod) (container_memory_working_set_bytes{' + csel + '})',
              ref="A", instant=True, fmt="table"),
            t('sum by (pod) (kube_pod_container_resource_requests{resource="memory", namespace=~"$namespace", pod=~"$pod"})',
              ref="B", instant=True, fmt="table"),
            t('sum by (pod) (container_memory_working_set_bytes{' + csel + '})'
              ' / sum by (pod) (kube_pod_container_resource_requests{resource="memory", namespace=~"$namespace", pod=~"$pod"})',
              ref="C", instant=True, fmt="table"),
            t('sum by (pod) (kube_pod_container_resource_limits{resource="memory", namespace=~"$namespace", pod=~"$pod"})',
              ref="D", instant=True, fmt="table"),
            t('sum by (pod) (container_memory_working_set_bytes{' + csel + '})'
              ' / sum by (pod) (kube_pod_container_resource_limits{resource="memory", namespace=~"$namespace", pod=~"$pod"})',
              ref="E", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "pod", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True,
                                  "Time 3": True, "Time 4": True, "Time 5": True},
                "renameByName": {"Value #A": "working set",
                                 "Value #B": "requests",
                                 "Value #C": "usage/requests",
                                 "Value #D": "limits",
                                 "Value #E": "usage/limits"}}},
        ],
        overrides=[
            override_props("working set", [{"id": "unit", "value": "bytes"}]),
            override_props("requests", [{"id": "unit", "value": "bytes"}]),
            override_props("limits", [{"id": "unit", "value": "bytes"}]),
            override_props("/^usage\\//", [
                {"id": "unit", "value": "percentunit"},
                {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, C_OK), (0.9, C_WARN), (1.05, C_ERR))},
            ], regex=True),
        ],
        sort_by=[{"displayName": "working set", "desc": True}],
        desc="working_set is what the OOM killer accounts against the limit.",
    )
    d.row("Quota tables", [(cpu_table, 12, 10), (mem_table, 12, 10)])
    d.row("Memory & network", [
        (timeseries("Memory working set by pod",
                    [t('sum by (pod) (container_memory_working_set_bytes{' + csel + '})',
                       "{{pod}}")], unit="bytes"), 8, 8),
        (timeseries("Pod network (rx +, tx −)",
                    [t('sum by (pod) (rate(container_network_receive_bytes_total{namespace=~"$namespace", pod=~"$pod"}[$__rate_interval]))',
                       "rx {{pod}}", ref="A"),
                     t('sum by (pod) (rate(container_network_transmit_bytes_total{namespace=~"$namespace", pod=~"$pod"}[$__rate_interval]))',
                       "tx {{pod}}", ref="B")],
                    unit="Bps", min0=False,
                    overrides=[override_props("/^tx .*/", [
                        {"id": "custom.transform", "value": "negative-Y"}],
                        regex=True)]), 8, 8),
        (timeseries("Restarts by pod (1h window)",
                    [t('sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace=~"$namespace", pod=~"$pod"}[1h]))',
                       "{{pod}}")], decimals=0), 8, 8),
    ])
    pod_table = table(
        "Pods — placement & age",
        [
            t('kube_pod_info{namespace=~"$namespace", pod=~"$pod"}',
              ref="A", instant=True, fmt="table"),
            t('(time() - kube_pod_start_time{namespace=~"$namespace", pod=~"$pod"})',
              ref="B", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField", "options": {"byField": "pod", "mode": "outer"}},
            {"id": "filterFieldsByName", "options": {"include": {
                "names": ["namespace 1", "pod", "node", "created_by_kind",
                          "Value #B"]}}},
            {"id": "organize", "options": {
                "renameByName": {"namespace 1": "namespace",
                                 "created_by_kind": "kind",
                                 "Value #B": "age"}}},
        ],
        overrides=[override_props("age", [{"id": "unit", "value": "s"}])],
        sort_by=[{"displayName": "age", "desc": False}],
    )
    d.row("Placement", [(pod_table, 24, 10)])
    return d


# =========================================================================== #
# 9. Istio · Mesh & Gateway
# =========================================================================== #

def istio_mesh():
    d = Dash(
        "istio-mesh", "Istio · Mesh & Gateway", ["istio"],
        time_from="now-3h", refresh="1m",
        desc="Mesh-wide RED (reporter=destination), gateway edge view, "
             "response flags, mTLS coverage, raw TCP (Mongo/Milvus/Postgres "
             "flows), and the istiod control plane.",
    )
    d.row("Mesh health", [
        (stat("Global request volume",
              [t('sum(rate(istio_requests_total{reporter="destination"}[5m]))')],
              unit="reqps", decimals=2), 4, 4),
        (stat("Global success rate",
              [t('100 * sum(rate(istio_requests_total{reporter="destination", response_code!~"5.."}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination"}[5m]))')],
              unit="percent", decimals=2,
              thr=steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))), 4, 4),
        (stat("4xx rate",
              [t('sum(rate(istio_requests_total{reporter="destination", response_code=~"4.."}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.5, C_WARN), (5, C_ERR))), 3, 4),
        (stat("5xx rate",
              [t('sum(rate(istio_requests_total{reporter="destination", response_code=~"5.."}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.01, C_WARN), (0.1, C_ERR))), 3, 4),
        (stat("mTLS traffic share",
              [t('100 * sum(rate(istio_requests_total{reporter="destination", connection_security_policy="mutual_tls"}[5m]))'
                 ' / sum(rate(istio_requests_total{reporter="destination"}[5m]))')],
              unit="percent", decimals=1,
              thr=steps((None, C_ERR), (95, C_WARN), (99.5, C_OK)),
              desc="Zero-trust check: in-mesh HTTP that actually rode mTLS."),
         4, 4),
        (stat("xDS-connected proxies", [t('sum(pilot_xds)')],
              thr=steps((None, "blue")), graph_mode="none"), 3, 4),
        (stat("Workload cert expiry (min)",
              [t('min(istio_agent_cert_expiry_seconds)')],
              unit="s", thr=steps((None, C_ERR), (3600, C_WARN), (14400, C_OK)),
              graph_mode="none",
              desc="Istio workload certs rotate continuously; sustained low "
                   "values mean rotation is stuck."), 3, 4),
    ])
    d.row("Ingress gateway (edge)", [
        (timeseries("Gateway request rate by destination",
                    [t('sum by (destination_service_name) (rate(istio_requests_total{reporter="source", source_workload="istio-ingress"}[$__rate_interval]))',
                       "{{destination_service_name}}")], unit="reqps"), 8, 8),
        (timeseries("Gateway 5xx by destination",
                    [t('sum by (destination_service_name) (rate(istio_requests_total{reporter="source", source_workload="istio-ingress", response_code=~"5.."}[$__rate_interval]))',
                       "{{destination_service_name}}")], unit="reqps"), 8, 8),
        (timeseries("Gateway P99 by destination",
                    [t('histogram_quantile(0.99, sum by (le, destination_service_name) (rate(istio_request_duration_milliseconds_bucket{reporter="source", source_workload="istio-ingress"}[$__rate_interval])))',
                       "{{destination_service_name}}")], unit="ms"), 8, 8),
    ])
    wl_table = table(
        "Workloads — RED (reporter=destination)",
        [
            t('sum by (destination_workload) (rate(istio_requests_total{reporter="destination"}[5m]))',
              ref="A", instant=True, fmt="table"),
            t('100 * sum by (destination_workload) (rate(istio_requests_total{reporter="destination", response_code!~"5.."}[5m]))'
              ' / sum by (destination_workload) (rate(istio_requests_total{reporter="destination"}[5m]))',
              ref="B", instant=True, fmt="table"),
            t('histogram_quantile(0.5, sum by (le, destination_workload) (rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[5m])))',
              ref="C", instant=True, fmt="table"),
            t('histogram_quantile(0.99, sum by (le, destination_workload) (rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[5m])))',
              ref="D", instant=True, fmt="table"),
        ],
        transformations=[
            {"id": "joinByField",
             "options": {"byField": "destination_workload", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Time 1": True, "Time 2": True,
                                  "Time 3": True, "Time 4": True},
                "renameByName": {"destination_workload": "workload",
                                 "Value #A": "req/s", "Value #B": "success %",
                                 "Value #C": "p50 (ms)", "Value #D": "p99 (ms)"}}},
        ],
        overrides=[
            override_props("req/s", [{"id": "unit", "value": "reqps"},
                                     {"id": "decimals", "value": 2}]),
            override_props("success %", [
                {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions",
                 "value": {"type": "color-background", "mode": "basic"}},
                {"id": "thresholds",
                 "value": steps((None, C_ERR), (99, C_WARN), (99.9, C_OK))},
            ]),
            override_props("p50 (ms)", [{"id": "unit", "value": "ms"},
                                        {"id": "decimals", "value": 1}]),
            override_props("p99 (ms)", [{"id": "unit", "value": "ms"},
                                        {"id": "decimals", "value": 1}]),
        ],
        sort_by=[{"displayName": "req/s", "desc": True}],
    )
    d.row("Workloads", [
        (wl_table, 12, 10),
        (timeseries("Service-to-service request rate",
                    [t('sum by (source_workload, destination_workload) (rate(istio_requests_total{reporter="destination", source_workload!="unknown"}[$__rate_interval]))',
                       "{{source_workload}} → {{destination_workload}}")],
                    unit="reqps",
                    desc="Every in-mesh HTTP edge (who calls whom)."), 12, 10),
    ])
    d.row("Failures & flags", [
        (timeseries("Response flags (reporter=source)",
                    [t('sum by (response_flags) (rate(istio_requests_total{reporter="source", response_flags!="-"}[$__rate_interval]))',
                       "{{response_flags}}")],
                    desc="UO=overflow/circuit-break NR=no route DC=downstream "
                         "close UF=upstream conn fail URX=retry exhausted. "
                         "Client-side Envoy sees failures the server never "
                         "logs."), 12, 8),
        (timeseries("Mesh 5xx by destination workload",
                    [t('sum by (destination_workload) (rate(istio_requests_total{reporter="destination", response_code=~"5.."}[$__rate_interval]))',
                       "{{destination_workload}}")], unit="reqps"), 12, 8),
    ])
    d.row("Raw TCP (databases & vector store)", [
        (timeseries("TCP bytes → destination (rx at server)",
                    [t('sum by (destination_canonical_service) (rate(istio_tcp_received_bytes_total{reporter="destination"}[$__rate_interval]))',
                       "{{destination_canonical_service}}")], unit="Bps",
                    desc="Mongo, Milvus and the CNPG Postgres flows ride "
                         "plain TCP through the mesh and show up here."), 12, 8),
        (timeseries("TCP bytes ← destination (tx from server)",
                    [t('sum by (destination_canonical_service) (rate(istio_tcp_sent_bytes_total{reporter="destination"}[$__rate_interval]))',
                       "{{destination_canonical_service}}")], unit="Bps"), 12, 8),
    ])
    d.row("Control plane (istiod)", [
        (timeseries("xDS pushes by type",
                    [t('sum by (type) (rate(pilot_xds_pushes[$__rate_interval]))',
                       "{{type}}")]), 8, 8),
        (timeseries("Proxy config convergence P99",
                    [t('histogram_quantile(0.99, sum by (le) (rate(pilot_proxy_convergence_time_bucket[$__rate_interval])))',
                       "p99")], unit="s",
                    overrides=[override_color("p99", P99)],
                    desc="Time from a config change to every proxy having it."),
         8, 8),
        (timeseries("istiod CPU & memory",
                    [t('sum(rate(container_cpu_usage_seconds_total{namespace="istio-system", pod=~"istiod-.*", container!=""}[$__rate_interval]))',
                       "cpu (cores)", ref="A"),
                     t('sum(container_memory_working_set_bytes{namespace="istio-system", pod=~"istiod-.*", container!=""}) / 1024 / 1024',
                       "memory (MiB)", ref="B")],
                    desc="Two units on one panel kept honest: cores vs MiB — "
                         "read each series against the legend, not the axis."),
         8, 8),
    ])
    return d


# =========================================================================== #
# 10. Identity & Policy (Keycloak + OPA)
# =========================================================================== #

def identity_policy():
    kc = 'job="keycloak"'
    d = Dash(
        "identity-policy", "Identity & Policy (Keycloak + OPA)",
        ["identity", "keycloak", "opa"],
        time_from="now-6h", refresh="1m",
        desc="The auth plane: Keycloak (OIDC issuer — request rates, token "
             "endpoint, JVM) and OPA (the mesh PDP — decision rate & latency). "
             "Keycloak's uri label is unbounded (raw scanner paths) so panels "
             "only ever filter on it, never group by it.",
    )
    d.row("Keycloak — OIDC issuer", [
        (stat("Request rate",
              [t(f'sum(rate(http_server_requests_seconds_count{{{kc}}}[5m]))')],
              unit="reqps", decimals=2), 4, 4),
        (stat("5xx rate",
              [t(f'sum(rate(http_server_requests_seconds_count{{{kc}, status=~"5.."}}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.01, C_WARN), (0.1, C_ERR))), 4, 4),
        (stat("Token issuance rate",
              [t(f'sum(rate(http_server_requests_seconds_count{{{kc}, uri=~".*openid-connect/token"}}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              desc="POST /realms/*/protocol/openid-connect/token — includes "
                   "every RFC 8693 service exchange, so this is the exchange "
                   "heartbeat of the platform."), 5, 4),
        (stat("Mean latency",
              [t(f'sum(rate(http_server_requests_seconds_sum{{{kc}}}[5m])) / sum(rate(http_server_requests_seconds_count{{{kc}}}[5m]))')],
              unit="s", thr=steps((None, C_OK), (0.25, C_WARN), (1, C_ERR)),
              desc="Micrometer here exposes no histogram buckets — mean + max "
                   "is what we get."), 4, 4),
        (stat("Password validations (5m)",
              [t('sum(increase(keycloak_credentials_password_hashing_validations_total[5m])) or vector(0)')],
              decimals=0, graph_mode="none",
              desc="Interactive logins hash a password; token exchanges "
                   "don't. A spike here = humans (or a brute force)."), 4, 4),
        (stat("Keycloak up", [t(f'up{{{kc}}}')],
              mappings=[{"type": "value",
                         "options": {"0": {"text": "DOWN", "color": "red"},
                                     "1": {"text": "UP", "color": "green"}}}],
              color_mode="background", graph_mode="none",
              thr=steps((None, C_ERR), (1, C_OK))), 3, 4),
    ])
    d.row("Keycloak traffic & runtime", [
        (timeseries("Requests by status",
                    [t(f'sum by (status) (rate(http_server_requests_seconds_count{{{kc}}}[$__rate_interval]))',
                       "{{status}}")], unit="reqps",
                    overrides=[override_color("/^2../", C_OK, regex=True),
                               override_color("/^3../", "purple", regex=True),
                               override_color("/^4../", C_WARN, regex=True),
                               override_color("/^5../", C_ERR, regex=True)]), 8, 8),
        (timeseries("OIDC endpoints",
                    [t(f'sum(rate(http_server_requests_seconds_count{{{kc}, uri=~".*openid-connect/token"}}[$__rate_interval]))', "token", ref="A"),
                     t(f'sum(rate(http_server_requests_seconds_count{{{kc}, uri=~".*openid-connect/auth.*"}}[$__rate_interval]))', "auth (browser)", ref="B"),
                     t(f'sum(rate(http_server_requests_seconds_count{{{kc}, uri=~".*openid-connect/certs"}}[$__rate_interval]))', "jwks", ref="C"),
                     t(f'sum(rate(http_server_requests_seconds_count{{{kc}, uri=~".*openid-connect/logout.*"}}[$__rate_interval]))', "logout", ref="D")],
                    unit="reqps"), 8, 8),
        (timeseries("JVM heap & latency max",
                    [t(f'sum(jvm_memory_used_bytes{{{kc}, area="heap"}})',
                       "heap used (bytes)", ref="A"),
                     t(f'max(http_server_requests_seconds_max{{{kc}}})',
                       "slowest request (s)", ref="B")],
                    desc="Mixed units, read per-legend: heap in bytes, "
                         "slowest-request in seconds."), 8, 8),
    ])
    # NOTE: OPA's http_request_duration_seconds only instruments its own HTTP
    # API (health/diagnostics) — the actual authz decisions arrive over gRPC
    # ext_authz and are NOT in that metric. Decision outcomes are observed
    # from the mesh side instead: a denial surfaces as Envoy response_flags
    # UAEX (unauthorized by external authz service).
    d.row("OPA — the policy decision point", [
        (stat("OPA instances up", [t('sum(up{job="opa"})')],
              thr=steps((None, C_ERR), (1, C_OK)), graph_mode="none"), 4, 4),
        (stat("Authz denials (mesh)",
              [t('sum(rate(istio_requests_total{reporter="source", response_flags="UAEX"}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.05, C_WARN), (0.5, C_ERR)),
              desc="Requests Envoy rejected at the ext_authz filter "
                   "(response_flags=UAEX): an OPA deny — or the authz plane "
                   "being unreachable, which fails closed."), 5, 4),
        (stat("OPA HTTP API P99",
              [t('histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="opa"}[5m])))')],
              unit="s", thr=steps((None, C_OK), (0.05, C_WARN), (0.25, C_ERR)),
              desc="OPA's HTTP endpoints only (health/diagnostics) — the gRPC "
                   "ext_authz decision path isn't in this metric. Still a "
                   "responsiveness canary for the process."), 5, 4),
        (stat("OPA HTTP non-2xx",
              [t('sum(rate(http_request_duration_seconds_count{job="opa", code!~"2.."}[5m])) or vector(0)')],
              unit="reqps", decimals=3,
              thr=steps((None, C_OK), (0.01, C_WARN))), 5, 4),
        (stat("OPA memory",
              [t('sum(container_memory_working_set_bytes{namespace="opa-system", container!=""})')],
              unit="bytes", graph_mode="none"), 5, 4),
    ])
    d.row("OPA detail", [
        (timeseries("Authz denials by destination workload",
                    [t('sum by (destination_workload) (rate(istio_requests_total{reporter="source", response_flags="UAEX"}[$__rate_interval]))',
                       "{{destination_workload}}")], unit="reqps",
                    desc="Who is being denied where. Empty = no denials in "
                         "the window (good)."), 8, 8),
        (timeseries("OPA HTTP API latency percentiles",
                    [t('histogram_quantile(0.5, sum by (le) (rate(http_request_duration_seconds_bucket{job="opa"}[$__rate_interval])))', "p50", ref="A"),
                     t('histogram_quantile(0.9, sum by (le) (rate(http_request_duration_seconds_bucket{job="opa"}[$__rate_interval])))', "p90", ref="B"),
                     t('histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="opa"}[$__rate_interval])))', "p99", ref="C")],
                    unit="s",
                    overrides=[override_color("p50", P50),
                               override_color("p90", P90),
                               override_color("p99", P99)]), 8, 8),
        (logspanel("OPA logs", '{service_name="opa"}'), 8, 8),
    ])
    d.row("Auth-plane pods", [
        (timeseries("Keycloak pod CPU / memory",
                    [t('sum(rate(container_cpu_usage_seconds_total{namespace="identity", pod=~"keycloak-.*", container!=""}[$__rate_interval]))',
                       "cpu (cores)", ref="A"),
                     t('sum(container_memory_working_set_bytes{namespace="identity", pod=~"keycloak-.*", container!=""}) / 1024 / 1024',
                       "memory (MiB)", ref="B")]), 12, 8),
        (timeseries("OPA pod CPU / memory",
                    [t('sum(rate(container_cpu_usage_seconds_total{namespace="opa-system", container!=""}[$__rate_interval]))',
                       "cpu (cores)", ref="A"),
                     t('sum(container_memory_working_set_bytes{namespace="opa-system", container!=""}) / 1024 / 1024',
                       "memory (MiB)", ref="B")]), 12, 8),
    ])
    return d


# =========================================================================== #
# 11. Postgres · CNPG
# =========================================================================== #

def cnpg():
    sel = 'cnpg_cluster=~"$cluster"'
    d = Dash(
        "cnpg-postgres", "Postgres · CNPG clusters",
        ["postgres", "cnpg", "database"],
        time_from="now-6h", refresh="1m",
        desc="CloudNativePG fleet: app-db, mail-db, jobs-db, agents-db (prod) "
             "and kc-db (identity). Transactions, connections, cache hits, "
             "WAL & archiving, database sizes.",
        variables=[
            qvar("cluster", "cluster",
                 "label_values(cnpg_collector_up, cnpg_cluster)", multi=True,
                 include_all=True, allv=".*",
                 current={"text": ["All"], "value": ["$__all"], "selected": True}),
        ],
    )
    d.row("Fleet health", [
        (stat("Instances up", [t(f'sum(cnpg_collector_up{{{sel}}})')],
              thr=steps((None, C_ERR), (5, C_OK)), graph_mode="none"), 3, 4),
        (stat("Total DB size",
              [t(f'sum(cnpg_pg_database_size_bytes{{{sel}, datname!~"template.*"}})')],
              unit="bytes"), 4, 4),
        (stat("Backends (connections)",
              [t(f'sum(cnpg_backends_total{{{sel}}})')], decimals=0), 3, 4),
        (stat("WAL archiving — time since last success",
              [t(f'max(cnpg_pg_stat_archiver_seconds_since_last_archival{{{sel}}})')],
              unit="s", thr=steps((None, C_OK), (600, C_WARN), (1800, C_ERR)),
              desc="Continuous archiving to object storage backs PITR; this "
                   "growing means backups are stale."), 5, 4),
        (stat("Last base backup age",
              [t(f'time() - max(cnpg_collector_last_available_backup_timestamp{{{sel}}})')],
              unit="s", thr=steps((None, C_OK), (172800, C_WARN), (345600, C_ERR))),
         5, 4),
        (stat("Deadlocks (1h)",
              [t(f'sum(increase(cnpg_pg_stat_database_deadlocks{{{sel}}}[1h])) or vector(0)')],
              decimals=0, thr=steps((None, C_OK), (1, C_ERR)),
              graph_mode="none"), 4, 4),
    ])
    d.row("Throughput", [
        (timeseries("Transactions — commits vs rollbacks",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_xact_commit{{{sel}}}[$__rate_interval]))',
                       "commit {{cnpg_cluster}}", ref="A"),
                     t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_xact_rollback{{{sel}}}[$__rate_interval]))',
                       "rollback {{cnpg_cluster}}", ref="B")],
                    unit="ops",
                    overrides=[override_props("/^rollback .*/", [
                        {"id": "color",
                         "value": {"mode": "fixed", "fixedColor": C_ERR}}],
                        regex=True)]), 12, 8),
        (timeseries("Buffer cache hit ratio",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_blks_hit{{{sel}}}[$__rate_interval]))'
                       f' / (sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_blks_hit{{{sel}}}[$__rate_interval]))'
                       f' + sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_blks_read{{{sel}}}[$__rate_interval])))',
                       "{{cnpg_cluster}}")], percent=True,
                    desc="Sustained <99% on these small DBs usually means "
                         "shared_buffers is too small or a seq-scan storm."),
         12, 8),
    ])
    d.row("Activity", [
        (timeseries("Tuple activity",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_tup_fetched{{{sel}}}[$__rate_interval]))', "fetched {{cnpg_cluster}}", ref="A"),
                     t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_tup_inserted{{{sel}}}[$__rate_interval]))', "inserted {{cnpg_cluster}}", ref="B"),
                     t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_tup_updated{{{sel}}}[$__rate_interval]))', "updated {{cnpg_cluster}}", ref="C"),
                     t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_tup_deleted{{{sel}}}[$__rate_interval]))', "deleted {{cnpg_cluster}}", ref="D")],
                    unit="ops"), 8, 8),
        (timeseries("Connections by cluster",
                    [t(f'sum by (cnpg_cluster) (cnpg_backends_total{{{sel}}})',
                       "{{cnpg_cluster}}")], decimals=0), 8, 8),
        (timeseries("Longest transaction",
                    [t(f'max by (cnpg_cluster) (cnpg_backends_max_tx_duration_seconds{{{sel}}})',
                       "{{cnpg_cluster}}")], unit="s",
                    desc="Long-running transactions block vacuum and bloat "
                         "the WAL."), 8, 8),
    ])
    d.row("WAL & storage", [
        (timeseries("WAL generated",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_collector_wal_bytes{{{sel}}}[$__rate_interval]))',
                       "{{cnpg_cluster}}")], unit="Bps"), 8, 8),
        (timeseries("WAL archiver — archived vs failed",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_archiver_archived_count{{{sel}}}[$__rate_interval]))',
                       "archived {{cnpg_cluster}}", ref="A"),
                     t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_archiver_failed_count{{{sel}}}[$__rate_interval]))',
                       "failed {{cnpg_cluster}}", ref="B")],
                    unit="ops",
                    overrides=[override_props("/^failed .*/", [
                        {"id": "color",
                         "value": {"mode": "fixed", "fixedColor": C_ERR}}],
                        regex=True)]), 8, 8),
        (timeseries("Database size",
                    [t(f'sum by (cnpg_cluster, datname) (cnpg_pg_database_size_bytes{{{sel}, datname!~"template.*|postgres"}})',
                       "{{cnpg_cluster}}/{{datname}}")], unit="bytes"), 8, 8),
    ])
    d.row("Instance pods", [
        (timeseries("CPU by database pod",
                    [t('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=~"prod|identity", pod=~"(app|mail|jobs|agents|kc)-db-.*", container!=""}[$__rate_interval]))',
                       "{{pod}}")], unit="cores", decimals=3), 8, 8),
        (timeseries("Memory by database pod",
                    [t('sum by (pod) (container_memory_working_set_bytes{namespace=~"prod|identity", pod=~"(app|mail|jobs|agents|kc)-db-.*", container!=""})',
                       "{{pod}}")], unit="bytes"), 8, 8),
        (timeseries("Temp files spilled to disk",
                    [t(f'sum by (cnpg_cluster) (rate(cnpg_pg_stat_database_temp_bytes{{{sel}}}[$__rate_interval]))',
                       "{{cnpg_cluster}}")], unit="Bps",
                    desc="Queries exceeding work_mem spill here — sustained "
                         "traffic means a query needs tuning."), 8, 8),
    ])
    return d


# --------------------------------------------------------------------------- #

DASHBOARDS = {
    "fm-platform-overview.json": fm_overview,
    "fm-service-drilldown.json": fm_service,
    "fm-canary.json": fm_canary,
    "fm-logs.json": fm_logs,
    "fm-traces.json": fm_traces,
    "fm-rum.json": fm_rum,
    "k8s-cluster.json": k8s_cluster,
    "k8s-pods.json": k8s_pods,
    "istio-mesh.json": istio_mesh,
    "identity-policy.json": identity_policy,
    "cnpg-postgres.json": cnpg,
}


def main():
    print("generating dashboards:")
    for fname, builder in DASHBOARDS.items():
        write(builder(), fname)


if __name__ == "__main__":
    main()
