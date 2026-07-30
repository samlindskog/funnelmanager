// Grafana Faro browser RUM — CANONICAL bootstrap (mailui/agentsui copy this file
// verbatim; keep it self-contained, version-pinned, and dependency-free).
// Signals: JS errors + web-vitals + session tracking + browser tracing, which
// auto-injects W3C `traceparent` into every fetch/XHR so a browser action and
// its backend spans join ONE trace. Query strings are scrubbed from every URL
// Faro captures (beforeSend) so an OIDC auth-code return URL or any query param
// never reaches the collector. Experimental OTel — fully guarded; RUM never
// breaks the app.
//
// Canonical-copy notes (mailui/agentsui replicate verbatim):
//   - Collector path is same-origin `/telemetry/collect`.
//   - Version comes from the Vite `define` global `__APP_VERSION__` (a string
//     literal injected at build time; see vite.config.ts + the ambient
//     `declare const` below). Only `app.name` changes per app.
import {
  initializeFaro,
  ErrorsInstrumentation,
  WebVitalsInstrumentation,
  SessionInstrumentation,
} from '@grafana/faro-web-sdk'
import type { TransportItem } from '@grafana/faro-web-sdk'
import { TracingInstrumentation } from '@grafana/faro-web-tracing'

// Injected at build time via Vite `define` (see vite.config.ts).
declare const __APP_VERSION__: string

// Drop the query string (and fragment) from a URL-ish string; never throws.
function stripQuery(url: string): string {
  const cut = url.search(/[?#]/)
  return cut === -1 ? url : url.slice(0, cut)
}

// Recursively walk any object/array, mutating in place: every URL-shaped
// (`://`) string leaf has its query/fragment stripped. Non-URL strings are
// left untouched. Depth-capped and cycle-guarded; only mutates string leaves.
// Cap must clear the deepest real payload: OTLP traces nest ~9 levels
// (payload→resourceSpans[]→scopeSpans[]→spans[]→attributes[]→value→stringValue).
function scrubNode(node: unknown, seen: Set<object>, depth: number): void {
  if (depth > 20 || node === null || typeof node !== 'object') {
    return
  }
  if (seen.has(node)) {
    return
  }
  seen.add(node)
  if (Array.isArray(node)) {
    for (let i = 0; i < node.length; i++) {
      const v = node[i]
      if (typeof v === 'string') {
        if (v.includes('://')) {
          node[i] = stripQuery(v)
        }
      } else {
        scrubNode(v, seen, depth + 1)
      }
    }
    return
  }
  const obj = node as Record<string, unknown>
  for (const key in obj) {
    const v = obj[key]
    if (typeof v === 'string') {
      if (v.includes('://')) {
        obj[key] = stripQuery(v)
      }
    } else {
      scrubNode(v, seen, depth + 1)
    }
  }
}

// Recursively strips the query/fragment from every URL-shaped (`://`) string in
// the item (page URLs, span/event attributes, exception stack-frame filenames)
// so no query param reaches the collector. Never throws; always returns item.
function scrubUrls(item: TransportItem): TransportItem {
  try {
    const seen = new Set<object>()
    scrubNode(item.meta, seen, 0)
    scrubNode(item.payload, seen, 0)
  } catch {
    // Defensive: telemetry scrubbing must never break the send path.
  }
  return item
}

export function initTelemetry(): void {
  try {
    initializeFaro({
      url: '/telemetry/collect',
      app: {
        name: 'agentsui',
        version: typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '0.0.0',
      },
      // Hand-listed to EXACTLY the locked signal set: errors + web-vitals +
      // session + browser tracing. Deliberately omits Performance (nav/resource
      // timing — leaks the OIDC ?code= return URL into an event), Console, View,
      // CSP, and UserAction (Phase 3). SessionInstrumentation + the
      // sessionTracking config together drive session tracking.
      instrumentations: [
        new ErrorsInstrumentation(),
        new WebVitalsInstrumentation(),
        new SessionInstrumentation(),
        new TracingInstrumentation(),
      ],
      sessionTracking: { enabled: true },
      beforeSend: scrubUrls,
    })
  } catch {
    // RUM is best-effort; a failed init must be a no-op, not an app crash.
  }
}
