import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initTelemetry } from './telemetry.ts'

// Start RUM before first render so instrumentation is active for the earliest
// fetches (traceparent injection + web-vitals). Guarded internally: never throws.
initTelemetry()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
