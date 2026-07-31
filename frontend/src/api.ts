import { clearTokens, config, getAccessToken, hasSession } from './oidc'
import type { AppLink } from './types'

/** Current access token (transparently refreshed via the OIDC session). */
export function getToken(): Promise<string | null> {
  return getAccessToken()
}

type UnauthorizedListener = () => void
const unauthorizedListeners = new Set<UnauthorizedListener>()

/** Subscribe to mid-session 401s (expired/revoked session) so auth state can
 * reset and the app can route back to login instead of surfacing raw
 * Unauthorized errors. Returns an unsubscribe function. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

function handleUnauthorized(): ApiError {
  clearTokens()
  for (const listener of unauthorizedListeners) listener()
  return new ApiError(401, 'Unauthorized', 'Unauthorized')
}

/** Access token for an API call. A session that exists but cannot be
 * refreshed right now (identity provider unreachable) is NOT a logout —
 * throw a retryable 503 and keep the tokens; only a genuinely absent
 * session resets auth state. Never send an authenticated endpoint a
 * token-less request: its 401 would be mistaken for session death. */
async function bearerToken(): Promise<string> {
  const token = await getToken()
  if (token) return token
  if (hasSession()) {
    const message = 'Could not refresh the session — retry shortly'
    throw new ApiError(503, message, message)
  }
  throw handleUnauthorized()
}

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

// ---------------------------------------------------------------------------
// Hub apps — static per-environment config (identity lives in Keycloak now,
// so there is no auth service to serve this list)
// ---------------------------------------------------------------------------

const DEFAULT_APPS: AppLink[] = [
  { name: 'Search', description: 'Apollo person/company search', url: '/search/', probe: '/api/search/whoami' },
  { name: 'Mail', description: 'Google Workspace inboxes & sending', url: '/mail/', probe: '/api/mail/whoami' },
  { name: 'Agents', description: 'AI agents that run tasks for you', url: '/agents/', probe: '/api/agents/whoami' },
]

export function fetchApps(): AppLink[] {
  const apps = config().apps
  return Array.isArray(apps) && apps.length
    ? apps.map((app) => ({ description: '', ...app }))
    : DEFAULT_APPS
}

/** Admin-only tiles for the hub's Administration section (e.g. Grafana),
 * configured via ADMIN_APPS. The Keycloak console tile is always shown. */
export function fetchAdminApps(): AppLink[] {
  const apps = config().adminApps
  return Array.isArray(apps) ? apps.map((app) => ({ description: '', ...app })) : []
}

/** Discovery: a tile is shown when its probe answers with anything but
 * 401/403 under the user's token — the backing service's own authz
 * (OPA/grants) is the source of truth, so nothing is duplicated here.
 * Tiles with a roles list (external apps that can't be probed cross-origin)
 * are filtered against the user's realm roles instead; tiles with neither
 * always show. Network failures fail open — the server still enforces. */
export async function filterAvailableApps(apps: AppLink[], roles: string[]): Promise<AppLink[]> {
  const visible = await Promise.all(
    apps.map(async (app) => {
      if (app.roles?.length) return app.roles.some((role) => roles.includes(role))
      if (!app.probe) return true
      try {
        const response = await fetch(app.probe, {
          headers: { Authorization: `Bearer ${await bearerToken()}` },
        })
        return response.status !== 401 && response.status !== 403
      } catch {
        return true
      }
    }),
  )
  return apps.filter((_, index) => visible[index])
}
