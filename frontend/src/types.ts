export interface User {
  username: string
  role: string
}

export interface AppLink {
  name: string
  description: string
  url: string
  /** Authenticated discovery probe (e.g. /api/search/whoami). 2xx shows the
   * tile, 401/403 hides it — the backend's authz is the source of truth. */
  probe?: string
  /** For external apps that can't be probed cross-origin (Grafana, Keycloak):
   * show the tile only if the user holds one of these realm roles. */
  roles?: string[]
}
