// Grafana Faro browser RUM — CANONICAL bootstrap (mailui/agentsui copy this file
// verbatim; keep it self-contained, version-pinned, and dependency-free).
// DEV/CANARY-ONLY: this module is imported ONLY by main.tsx, and ONLY via a
// dynamic import behind a statically-false gate, so a prod build tree-shakes it
// (and all @grafana/faro-* deps) out entirely. Never import it statically.
// Signals: JS errors + web-vitals + session tracking + browser tracing (which
// auto-injects W3C `traceparent` into most fetches/XHRs so a browser action and
// its backend spans join ONE trace) + user actions (clicks/keydown → named
// events; see below). Because init is via a DYNAMIC import (async), the very
// first bootstrap request can fire before tracing is active and miss its
// traceparent; subsequent fetches are covered. Query strings are scrubbed from
// every URL Faro captures (beforeSend) so an OIDC auth-code return URL or any
// query param never reaches the collector. Experimental OTel — fully guarded;
// RUM never breaks the app.
//
// USER-ACTION NAMING (the canonical id contract) — one id per action drives
// BOTH the `data-testid` (for tests) AND the Faro user-action event name.
// Mechanism: Faro 2.9.0's UserActionInstrumentation names an action by reading
// a data-* attribute off the `pointerdown`/keydown target element. We point it
// at `data-testid` via `userActionsInstrumentation.dataAttributeName` below, so
// authoring a single `data-testid` on an interactive element yields both.
// KNOWN LIMITATION: Faro reads only the EXACT event.target's own dataset (no
// ancestor traversal). Text `<Button>`s resolve fine — MUI's ripple overlay is
// pointer-events:none, so the pointer target is the root button that carries
// the id. Icon-only buttons (`<IconButton>`/`<Fab>`) may NOT emit a named
// user-action event: the pointer target is often the child `<svg>`, which has
// no data-testid, so Faro finds no name and drops the action. The data-testid
// still serves Playwright/tests on those elements regardless of the Faro event.
// Convention: lowercase kebab, `<area>-<action>[-<qualifier>]`. Areas:
//   hub-*     landing / tiles / sign-in-out (HubPage, LandingPage, CallbackPage)
//   search-*  the search app (SearchPage + Search* components)
//   record-*  the record-detail pane
// (`color-mode-toggle` is a shared chrome control with no area prefix.)
// Examples: hub-signin, hub-open-search, search-run, search-cancel,
// search-enrich, search-history-delete, search-page-next, record-refresh.
//
// Canonical-copy notes (mailui/agentsui replicate verbatim):
//   - Collector path is same-origin `/telemetry/collect`.
//   - Version comes from the Vite `define` global `__APP_VERSION__` (a string
//     literal injected at build time; see vite.config.ts + the ambient
//     `declare const` below). Only `app.name` changes per app.
//   - The `dataAttributeName: 'data-testid'` binding is identical per app; only
//     the testid VALUES are app-specific.
import {
  initializeFaro,
  ErrorsInstrumentation,
  WebVitalsInstrumentation,
  SessionInstrumentation,
  UserActionInstrumentation,
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
      // session + browser tracing + user actions (Phase 3). Deliberately omits
      // Performance (nav/resource timing — leaks the OIDC ?code= return URL into
      // an event), Console, View, and CSP. SessionInstrumentation + the
      // sessionTracking config together drive session tracking.
      instrumentations: [
        new ErrorsInstrumentation(),
        new WebVitalsInstrumentation(),
        new SessionInstrumentation(),
        new TracingInstrumentation(),
        new UserActionInstrumentation(),
      ],
      // Name user actions from the SAME `data-testid` used for tests, so one
      // authored id drives both (see the USER-ACTION NAMING header above).
      userActionsInstrumentation: { dataAttributeName: 'data-testid' },
      sessionTracking: { enabled: true },
      beforeSend: scrubUrls,
    })
  } catch {
    // RUM is best-effort; a failed init must be a no-op, not an app crash.
  }
}
