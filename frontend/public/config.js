// Runtime configuration — replaced per environment (the prod image's
// entrypoint regenerates this file from env vars; this copy is the dev
// default served by Vite / the dev nginx).
window.__FM_CONFIG__ = {
  oidcIssuer: 'http://localhost:8080/realms/funnelmanager',
  oidcClientId: 'frontend',
  // Hub tiles (probe-gated by each service's /whoami). Setting apps replaces
  // the built-in Search+Mail default, so all three are listed here.
  apps: [
    { name: 'Search', description: 'Apollo person/company search', url: '/search', probe: '/api/search/whoami' },
    { name: 'Mail', description: 'Google Workspace inboxes & sending', url: '/mail/', probe: '/api/mail/whoami' },
    { name: 'Agents', description: 'AI agents that run tasks for you', url: '/agents/', probe: '/api/agents/whoami' },
  ],
}
