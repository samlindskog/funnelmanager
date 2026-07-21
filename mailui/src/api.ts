/** Mail app API client.
 *
 * The app is served same-origin at /mail/ and shares the hub's Keycloak OIDC
 * session (localStorage fm_oidc_* keys) — whichever app is open refreshes it.
 * There is no login form here: a missing/expired session redirects straight
 * to Keycloak and returns to /mail/ afterwards.
 */

import { beginLogin, clearTokens, getAccessToken, hasSession, logout as oidcLogout } from './oidc'
import type {
  MailAccount,
  MailAttachment,
  MailMessageDetail,
  MailMessagePage,
  MailSendRequest,
} from './types'

export function redirectToLogin(): void {
  void beginLogin('/mail/')
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
  const token = await getAccessToken()
  if (!token) {
    if (hasSession()) {
      // The session exists but could not be refreshed right now (identity
      // provider unreachable) — retryable, keep the tokens, do NOT log out.
      const message = 'Could not refresh the session — retry shortly'
      throw new ApiError(503, message, message)
    }
    redirectToLogin()
    throw new ApiError(401, 'Unauthorized', 'Unauthorized')
  }
  headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { ...init, headers })
  // Session expired/revoked beyond refresh — sign in again.
  if (response.status === 401) {
    clearTokens()
    redirectToLogin()
    throw new ApiError(401, 'Unauthorized', 'Unauthorized')
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
// Session (Keycloak OIDC, shared same-origin with the hub)
// ---------------------------------------------------------------------------

/** RP-initiated logout: ends the Keycloak session and leaves this app. */
export function logout(): void {
  oidcLogout()
}

// ---------------------------------------------------------------------------
// Mail
// ---------------------------------------------------------------------------

export async function fetchMailAccounts(): Promise<MailAccount[]> {
  return request<MailAccount[]>('/api/mail/accounts')
}

export async function deleteMailAccount(accountId: number): Promise<void> {
  await request<void>(`/api/mail/accounts/${accountId}`, { method: 'DELETE' })
}

export async function triggerMailSync(accountId: number): Promise<void> {
  await request<{ status: string }>(`/api/mail/accounts/${accountId}/sync`, {
    method: 'POST',
  })
}

/** Start the Google consent flow: the caller navigates to the returned URL. */
export async function fetchMailOauthUrl(): Promise<string> {
  const data = await request<{ url: string }>('/api/mail/oauth/url')
  return data.url
}

export type MailListParams = {
  accountId: number
  label: string
  q?: string
  page?: number
  perPage?: number
}

export async function fetchMailMessages(params: MailListParams): Promise<MailMessagePage> {
  const search = new URLSearchParams()
  search.set('label', params.label)
  if (params.q) search.set('q', params.q)
  search.set('page', String(params.page ?? 1))
  search.set('per_page', String(params.perPage ?? 50))
  return request<MailMessagePage>(`/api/mail/accounts/${params.accountId}/messages?${search}`)
}

export async function fetchMailMessage(messageId: number): Promise<MailMessageDetail> {
  return request<MailMessageDetail>(`/api/mail/messages/${messageId}`)
}

export async function sendMailMessage(
  accountId: number,
  body: MailSendRequest,
): Promise<MailMessageDetail> {
  return request<MailMessageDetail>(`/api/mail/accounts/${accountId}/send`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** Authenticated attachment download: fetch as a blob and trigger a save. */
export async function downloadMailAttachment(
  messageId: number,
  attachment: MailAttachment,
): Promise<void> {
  const token = await getAccessToken()
  const response = await fetch(
    `/api/mail/messages/${messageId}/attachments/${encodeURIComponent(attachment.attachment_id)}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : undefined },
  )
  if (response.status === 401) {
    clearTokens()
    redirectToLogin()
    return
  }
  if (!response.ok) {
    throw new ApiError(response.status, 'Download failed', `Download failed (${response.status})`)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = attachment.filename || 'attachment'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
