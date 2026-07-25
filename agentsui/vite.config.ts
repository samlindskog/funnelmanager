import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

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
  // The agents app lives under /agents/ behind the main nginx — same origin as
  // the hub, so the fm_oidc_* session in localStorage is shared.
  base: '/agents/',
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
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8006',
        changeOrigin: true,
      },
    },
  },
})
