/** Mail app API client.
 *
 * The app is served same-origin at /mail/, so it shares the hub's session
 * token (localStorage `fm_token`). There is no login page here — an expired
 * or missing session sends the browser back to the hub's /login.
 */

import type {
  MailAccount,
  MailAttachment,
  MailMessageDetail,
  MailMessagePage,
  MailSendRequest,
  User,
} from './types'

const TOKEN_KEY = 'fm_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function redirectToLogin(): void {
  window.location.href = '/login'
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
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { ...init, headers })
  // Session expired/revoked (or never present) — back to the hub login.
  if (response.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
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
// Session (issued/managed by the hub; consumed here)
// ---------------------------------------------------------------------------

export async function fetchMe(): Promise<User> {
  return request<User>('/api/auth/me')
}

/** Best-effort server-side revoke, then back to the hub login. */
export async function logout(): Promise<void> {
  const token = getToken()
  if (token) {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
    } catch {
      /* ignore — logout is best-effort */
    }
  }
  localStorage.removeItem(TOKEN_KEY)
  redirectToLogin()
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
  const token = getToken()
  const response = await fetch(
    `/api/mail/messages/${messageId}/attachments/${encodeURIComponent(attachment.attachment_id)}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : undefined },
  )
  if (response.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
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
