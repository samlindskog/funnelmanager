/** Keycloak OIDC auth-code + PKCE client (no dependencies).
 *
 * Tokens are kept in localStorage under fm_oidc_* keys shared same-origin
 * with the mail app, so whichever app is open can refresh the session and
 * the other picks it up. Runtime configuration comes from /config.js
 * (window.__FM_CONFIG__), generated per environment — nothing is baked into
 * the bundle.
 */

export interface FmConfig {
  oidcIssuer: string
  oidcClientId: string
  apps?: { name: string; description?: string; url: string }[]
  // Admin-only tiles shown in the hub's Administration section (e.g. Grafana).
  adminApps?: { name: string; description?: string; url: string }[]
}

declare global {
  interface Window {
    __FM_CONFIG__?: FmConfig
  }
}

const DEFAULTS: FmConfig = {
  oidcIssuer: 'http://localhost:8080/realms/funnelmanager',
  oidcClientId: 'frontend',
}

export function config(): FmConfig {
  return { ...DEFAULTS, ...(window.__FM_CONFIG__ ?? {}) }
}

const ACCESS_KEY = 'fm_oidc_access'
const REFRESH_KEY = 'fm_oidc_refresh'
const ID_KEY = 'fm_oidc_id'
const EXPIRES_KEY = 'fm_oidc_expires_at'
// Per-tab (sessionStorage): the in-flight authorization request.
const VERIFIER_KEY = 'fm_oidc_verifier'
const STATE_KEY = 'fm_oidc_state'
const RETURN_KEY = 'fm_oidc_return_to'

const redirectUri = () => `${window.location.origin}${import.meta.env.BASE_URL}callback`
const authUrl = () => `${config().oidcIssuer}/protocol/openid-connect/auth`
const tokenUrl = () => `${config().oidcIssuer}/protocol/openid-connect/token`
const logoutUrl = () => `${config().oidcIssuer}/protocol/openid-connect/logout`

function b64url(bytes: Uint8Array): string {
  let text = ''
  for (const byte of bytes) text += String.fromCharCode(byte)
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function randomString(bytes = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(bytes)))
}

async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return b64url(new Uint8Array(digest))
}

export interface Claims {
  sub: string
  username: string
  roles: string[]
}

export function parseClaims(token: string): Claims | null {
  try {
    let payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    payload += '='.repeat((4 - (payload.length % 4)) % 4)
    // atob yields Latin-1 code units; decode the underlying bytes as UTF-8
    // so non-ASCII usernames/roles survive intact.
    const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0))
    const claims = JSON.parse(new TextDecoder().decode(bytes))
    return {
      sub: String(claims.sub ?? ''),
      username: String(claims.preferred_username ?? claims.sub ?? ''),
      roles: Array.isArray(claims.realm_access?.roles) ? claims.realm_access.roles : [],
    }
  } catch {
    return null
  }
}

function storeTokens(data: {
  access_token: string
  refresh_token?: string
  id_token?: string
  expires_in?: number
}): void {
  localStorage.setItem(ACCESS_KEY, data.access_token)
  if (data.refresh_token) localStorage.setItem(REFRESH_KEY, data.refresh_token)
  if (data.id_token) localStorage.setItem(ID_KEY, data.id_token)
  const lifetime = (data.expires_in ?? 60) * 1000
  localStorage.setItem(EXPIRES_KEY, String(Date.now() + lifetime))
}

export function clearTokens(): void {
  for (const key of [ACCESS_KEY, REFRESH_KEY, ID_KEY, EXPIRES_KEY]) localStorage.removeItem(key)
}

/** Redirect to Keycloak's authorization endpoint (never resolves). */
export async function beginLogin(returnTo?: string): Promise<void> {
  const verifier = randomString(48)
  const state = randomString(16)
  sessionStorage.setItem(VERIFIER_KEY, verifier)
  sessionStorage.setItem(STATE_KEY, state)
  sessionStorage.setItem(
    RETURN_KEY,
    returnTo ?? window.location.pathname + window.location.search,
  )
  const params = new URLSearchParams({
    client_id: config().oidcClientId,
    redirect_uri: redirectUri(),
    response_type: 'code',
    scope: 'openid profile',
    state,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: 'S256',
  })
  window.location.assign(`${authUrl()}?${params}`)
}

/** Handle the ?code=...&state=... redirect. Returns the path to resume at. */
export async function completeLogin(): Promise<string> {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const state = params.get('state')
  const verifier = sessionStorage.getItem(VERIFIER_KEY)
  const expectedState = sessionStorage.getItem(STATE_KEY)
  const returnTo = sessionStorage.getItem(RETURN_KEY) || import.meta.env.BASE_URL
  sessionStorage.removeItem(VERIFIER_KEY)
  sessionStorage.removeItem(STATE_KEY)
  sessionStorage.removeItem(RETURN_KEY)
  if (!code || !verifier || !state || state !== expectedState) {
    throw new Error('Login callback is missing or mismatched (retry signing in)')
  }
  const response = await fetch(tokenUrl(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: config().oidcClientId,
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    }),
  })
  if (!response.ok) throw new Error(`Token exchange failed (${response.status})`)
  storeTokens(await response.json())
  return returnTo
}

let refreshInFlight: Promise<string | null> | null = null

async function refreshTokens(): Promise<string | null> {
  const refreshToken = localStorage.getItem(REFRESH_KEY)
  if (!refreshToken) {
    // A stale access token with no refresh token is unrecoverable — clear it
    // so hasSession() reflects reality and callers route back to login.
    if (localStorage.getItem(ACCESS_KEY)) clearTokens()
    return null
  }
  try {
    const response = await fetch(tokenUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: config().oidcClientId,
        refresh_token: refreshToken,
      }),
    })
    if (!response.ok) {
      // Refresh token expired/revoked — the session is over.
      if (response.status === 400 || response.status === 401) clearTokens()
      return null
    }
    const data = await response.json()
    storeTokens(data)
    return data.access_token as string
  } catch {
    return null // transient network failure: keep tokens, caller may retry
  }
}

/** Current access token, transparently refreshed. Null = signed out. */
export async function getAccessToken(): Promise<string | null> {
  const access = localStorage.getItem(ACCESS_KEY)
  const expiresAt = Number(localStorage.getItem(EXPIRES_KEY) || 0)
  if (access && Date.now() < expiresAt - 30_000) return access
  refreshInFlight ??= refreshTokens().finally(() => {
    refreshInFlight = null
  })
  return refreshInFlight
}

/** Claims of the current session (without forcing a refresh). */
export function currentClaims(): Claims | null {
  const access = localStorage.getItem(ACCESS_KEY)
  return access ? parseClaims(access) : null
}

export function hasSession(): boolean {
  return Boolean(localStorage.getItem(ACCESS_KEY) || localStorage.getItem(REFRESH_KEY))
}

/** RP-initiated logout: clear local tokens, end the Keycloak session. */
export function logout(): void {
  const idToken = localStorage.getItem(ID_KEY)
  clearTokens()
  const params = new URLSearchParams({
    client_id: config().oidcClientId,
    post_logout_redirect_uri: window.location.origin + import.meta.env.BASE_URL,
  })
  if (idToken) params.set('id_token_hint', idToken)
  window.location.assign(`${logoutUrl()}?${params}`)
}
