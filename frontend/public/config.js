// Runtime configuration — replaced per environment (the prod image's
// entrypoint regenerates this file from env vars; this copy is the dev
// default served by Vite / the dev nginx).
window.__FM_CONFIG__ = {
  oidcIssuer: 'http://localhost:8080/realms/funnelmanager',
  oidcClientId: 'frontend',
  // apps: [{ name, description, url }] — omit to use the built-in defaults
  // (Search at /search, Mail at /mail/).
}
