import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// Telemetry (Faro RUM) is DEV/CANARY-ONLY and MUST NOT ship to production.
// The gate below is statically evaluable so a normal prod build (DEV=false,
// VITE_TELEMETRY unset) collapses to `false` and Rollup dead-code-eliminates
// the dynamic `import('./telemetry')` — dropping telemetry.ts and every
// @grafana/faro-* dependency from the prod bundle (verify: grep dist/ for
// "faro"/"grafana" → zero hits). `telemetry.ts` is imported ONLY here, and
// ONLY dynamically, so nothing anchors it into the graph statically.
//   - import.meta.env.DEV      → true under `vite` dev server (local dev on).
//   - VITE_TELEMETRY === '1'   → the Phase-4 canary build arg (canary on).
// Telemetry initializes ASYNCHRONOUSLY: this is a dynamic import, so
// createRoot().render() below runs before the telemetry chunk resolves. Most
// fetches (traceparent injection + web-vitals) are still captured, but the very
// first bootstrap request may fire before instrumentation is active and miss
// its traceparent. The dynamic import never throws.
if (import.meta.env.DEV || import.meta.env.VITE_TELEMETRY === '1') {
  import('./telemetry.ts')
    .then((m) => m.initTelemetry())
    .catch(() => {})
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
