/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Canary build flag. When `'1'` at build time, the telemetry gate in
   * `main.tsx` stays live so a production build INCLUDES Faro (Phase-4 canary).
   * Unset in a normal prod build → the gate is statically false and Rollup
   * dead-code-eliminates `telemetry.ts` + all `@grafana/faro-*` deps.
   */
  readonly VITE_TELEMETRY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
