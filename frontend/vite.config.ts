import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || 5173)

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // When Vite sits behind the docker nginx gateway, HMR websockets must
    // reconnect on the public nginx port rather than the container port.
    hmr: {
      clientPort: hmrClientPort,
    },
    proxy: {
      '/api': {
        // Non-Docker local runs hit uvicorn directly; in Docker compose the
        // browser talks to nginx, so this proxy is mainly a fallback.
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure: (proxy) => {
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
