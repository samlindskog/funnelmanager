import { createRequire } from 'node:module'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build-time app version for telemetry (Faro `app.version`). Prefer the
// CI/Docker build arg (APP_VERSION), fall back to package.json, then a constant.
const requireJson = createRequire(import.meta.url)
const pkgVersion = (requireJson('./package.json') as { version?: string }).version
const appVersion = process.env.APP_VERSION ?? pkgVersion ?? '0.0.0'

const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || 5173)
// When served through a TLS-terminating reverse proxy (e.g. Cloudflare in front
// of this dev server), HMR must reconnect over wss on the public port.
const hmrProtocol = process.env.VITE_HMR_PROTOCOL || undefined
// Hosts Vite will answer for when reverse-proxied; Vite blocks unknown Host
// headers otherwise. Comma-separated (e.g. "dev.example.com").
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS || '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  // The search app lives under /search/ behind the main nginx — same origin as
  // the hub, so the fm_oidc_* session in localStorage is shared.
  base: '/search/',
  server: {
    host: '0.0.0.0',
    port: 5173,
    ...(allowedHosts.length ? { allowedHosts } : {}),
    // When Vite sits behind the docker nginx gateway, HMR websockets must
    // reconnect on the public nginx port rather than the container port.
    hmr: {
      clientPort: hmrClientPort,
      ...(hmrProtocol ? { protocol: hmrProtocol } : {}),
    },
    proxy: {
      '/api': {
        // Non-Docker local runs hit uvicorn directly; in Docker compose the
        // browser talks to nginx, so this proxy is mainly a fallback.
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure: (proxy) => {
          // Search/enrich are NDJSON streams — strip content-length and disable
          // caching so the dev proxy flushes chunks as they arrive.
          proxy.on('proxyRes', (proxyRes) => {
            const contentType = proxyRes.headers['content-type'] || ''
            if (String(contentType).includes('application/x-ndjson')) {
              proxyRes.headers['cache-control'] = 'no-cache'
              delete proxyRes.headers['content-length']
            }
          })
        },
      },
    },
  },
})
