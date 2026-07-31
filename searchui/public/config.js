// Runtime configuration — replaced per environment (the prod image's
// entrypoint regenerates this file from env vars; this copy is the dev
// default served by Vite / the dev nginx). Same OIDC session as the hub
// (shared localStorage fm_oidc_* keys, same public client).
window.__FM_CONFIG__ = {
  oidcIssuer: 'http://localhost:8080/realms/funnelmanager',
  oidcClientId: 'frontend',
}
