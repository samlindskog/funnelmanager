import type {
  AccountRequest,
  AppLink,
  ChannelRequest,
  Grant,
  Role,
  User,
  UserDetail,
} from './types'

const TOKEN_KEY = 'fm_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
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
  setToken(null)
  for (const listener of unauthorizedListeners) listener()
  return new ApiError(401, 'Unauthorized', 'Unauthorized')
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

function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const value = detail as Record<string, unknown>
    if (typeof value.message === 'string') return value.message
  }
  if (detail != null) {
    try {
      return JSON.stringify(detail)
    } catch {
      /* ignore */
    }
  }
  return fallback
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const hadToken = Boolean(token)
  const response = await fetch(path, { ...init, headers })
  // A 401 on an authenticated request means the session expired/was revoked —
  // reset auth state and route back to login. A 401 on an unauthenticated
  // request (e.g. a bad login) is a normal error whose server message we keep.
  if (response.status === 401 && hadToken) {
    throw handleUnauthorized()
  }
  if (!response.ok) {
    let detail: unknown = `Request failed (${response.status})`
    try {
      const data = await response.json()
      detail = data.detail ?? detail
    } catch {
      /* ignore */
    }
    throw new ApiError(response.status, detail, detailMessage(detail, `Request failed (${response.status})`))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export async function login(username: string, password: string): Promise<string> {
  const body = new URLSearchParams()
  body.set('username', username)
  body.set('password', password)
  const data = await request<{ access_token: string }>('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  setToken(data.access_token)
  return data.access_token
}

export async function fetchMe(): Promise<User> {
  return request<User>('/api/auth/me')
}

/** Best-effort server-side session revoke. Failures are ignored — the caller
 * clears the local token regardless, so the user is logged out either way. */
export async function apiLogout(): Promise<void> {
  const token = getToken()
  if (!token) return
  try {
    await fetch('/api/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    /* ignore — logout is best-effort */
  }
}

export async function requestAccount(username: string): Promise<void> {
  await request<{ status: string }>('/api/auth/request-account', {
    method: 'POST',
    body: JSON.stringify({ username }),
  })
}

export async function fetchApps(): Promise<AppLink[]> {
  return request<AppLink[]>('/api/auth/apps')
}

// ---------------------------------------------------------------------------
// Admin — users
// ---------------------------------------------------------------------------

export async function listUsers(): Promise<UserDetail[]> {
  return request<UserDetail[]>('/api/auth/admin/users')
}

export async function createUser(
  username: string,
  password: string,
  role: string,
): Promise<UserDetail> {
  return request<UserDetail>('/api/auth/admin/users', {
    method: 'POST',
    body: JSON.stringify({ username, password, role }),
  })
}

export async function updateUser(
  username: string,
  fields: { password?: string; role?: string },
): Promise<UserDetail> {
  return request<UserDetail>(`/api/auth/admin/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    body: JSON.stringify(fields),
  })
}

export async function deleteUser(username: string): Promise<void> {
  await request<void>(`/api/auth/admin/users/${encodeURIComponent(username)}`, {
    method: 'DELETE',
  })
}

export async function unlinkChannel(
  username: string,
  channel: string,
  deviceId: string,
): Promise<void> {
  await request<void>(
    `/api/auth/admin/users/${encodeURIComponent(username)}/channels/${encodeURIComponent(channel)}/${encodeURIComponent(deviceId)}`,
    { method: 'DELETE' },
  )
}

// ---------------------------------------------------------------------------
// Admin — roles
// ---------------------------------------------------------------------------

export async function listRoles(): Promise<Role[]> {
  return request<Role[]>('/api/auth/admin/roles')
}

export async function createRole(
  name: string,
  description: string,
  grants: Grant[],
): Promise<Role> {
  return request<Role>('/api/auth/admin/roles', {
    method: 'POST',
    body: JSON.stringify({ name, description, grants }),
  })
}

export async function deleteRole(name: string): Promise<void> {
  await request<void>(`/api/auth/admin/roles/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
}

// ---------------------------------------------------------------------------
// Admin — pending requests
// ---------------------------------------------------------------------------

export async function listAccountRequests(): Promise<AccountRequest[]> {
  return request<AccountRequest[]>('/api/auth/admin/account-requests')
}

export async function approveAccountRequest(
  username: string,
  password: string,
  role: string,
): Promise<UserDetail> {
  return request<UserDetail>('/api/auth/admin/account-requests/approve', {
    method: 'POST',
    body: JSON.stringify({ username, password, role }),
  })
}

export async function denyAccountRequest(username: string): Promise<void> {
  await request<void>('/api/auth/admin/account-requests/deny', {
    method: 'POST',
    body: JSON.stringify({ username }),
  })
}

export async function listChannelRequests(): Promise<ChannelRequest[]> {
  return request<ChannelRequest[]>('/api/auth/admin/channel-requests')
}

export async function assignChannelRequest(payload: {
  channel: string
  device_id: string
  username?: string
  new_user?: { username: string; password: string; role: string }
}): Promise<UserDetail> {
  return request<UserDetail>('/api/auth/admin/channel-requests/assign', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function denyChannelRequest(
  channel: string,
  deviceId: string,
): Promise<void> {
  await request<void>('/api/auth/admin/channel-requests/deny', {
    method: 'POST',
    body: JSON.stringify({ channel, device_id: deviceId }),
  })
}
