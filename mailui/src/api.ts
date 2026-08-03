/** Mail app API client.
 *
 * The app is served same-origin at /mail/ and shares the hub's Keycloak OIDC
 * session (localStorage fm_oidc_* keys) — whichever app is open refreshes it.
 * There is no login form here: a missing/expired session redirects straight
 * to Keycloak and returns to /mail/ afterwards.
 */

import { beginLogin, clearTokens, getAccessToken, hasSession, logout as oidcLogout } from './oidc'
import type {
  BackupEstimate,
  BackupStart,
  Campaign,
  CampaignCreate,
  CampaignSettings,
  CampaignSourceIn,
  ConfirmationRequired,
  MailAccount,
  MailAttachment,
  MailMessageDetail,
  MailMessagePage,
  MailSendRequest,
  MailThread,
  SavedSearch,
  SourceMerge,
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

/**
 * If `err` is a 409 whose structured detail is a Principle-4
 * `confirmation_required` (the human confirm=true flow), return that detail;
 * otherwise null. `human_approval_required` (agent-origin only) is deliberately
 * NOT matched — a human UI cannot self-approve an agent action, so it surfaces
 * as an ordinary error message.
 */
export function confirmationRequired(err: unknown): ConfirmationRequired | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const detail = err.detail
  if (
    detail &&
    typeof detail === 'object' &&
    (detail as Record<string, unknown>).error === 'confirmation_required' &&
    typeof (detail as Record<string, unknown>).confirm_token === 'string'
  ) {
    return detail as unknown as ConfirmationRequired
  }
  return null
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

/** Aggregated view across ALL connected mailboxes/domains (unified inbox). */
export async function fetchAllMailMessages(
  params: Omit<MailListParams, 'accountId'>,
): Promise<MailMessagePage> {
  const search = new URLSearchParams()
  search.set('label', params.label)
  if (params.q) search.set('q', params.q)
  search.set('page', String(params.page ?? 1))
  search.set('per_page', String(params.perPage ?? 50))
  return request<MailMessagePage>(`/api/mail/messages?${search}`)
}

export async function fetchMailMessage(messageId: number): Promise<MailMessageDetail> {
  return request<MailMessageDetail>(`/api/mail/messages/${messageId}`)
}

/** Every archived message of one Gmail thread on an account, oldest first. */
export async function fetchMailThread(
  accountId: number,
  threadId: string,
): Promise<MailThread> {
  return request<MailThread>(
    `/api/mail/accounts/${accountId}/threads/${encodeURIComponent(threadId)}`,
  )
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

// ---------------------------------------------------------------------------
// Full-backup gate (Principle 4)
// ---------------------------------------------------------------------------

export async function fetchBackupEstimate(accountId: number): Promise<BackupEstimate> {
  return request<BackupEstimate>(`/api/mail/accounts/${accountId}/backup`)
}

/**
 * Authorize the full-mailbox archive. Over the size threshold the backend
 * answers 409 `confirmation_required`; the caller re-invokes with the echoed
 * `confirm_token` and `confirm=true` (the human confirm flow — see
 * `confirmationRequired`).
 */
export async function startBackup(
  accountId: number,
  opts: { confirm?: boolean; confirmToken?: string } = {},
): Promise<BackupStart> {
  const search = new URLSearchParams()
  if (opts.confirm) search.set('confirm', 'true')
  if (opts.confirmToken) search.set('confirm_token', opts.confirmToken)
  const qs = search.toString()
  return request<BackupStart>(
    `/api/mail/accounts/${accountId}/backup${qs ? `?${qs}` : ''}`,
    { method: 'POST' },
  )
}

// ---------------------------------------------------------------------------
// Campaigns (separate from the inbox; cross-user per Principle 1)
// ---------------------------------------------------------------------------

export async function fetchCampaigns(username?: string): Promise<Campaign[]> {
  const search = new URLSearchParams()
  if (username) search.set('username', username)
  const qs = search.toString()
  return request<Campaign[]>(`/api/mail/campaigns${qs ? `?${qs}` : ''}`)
}

export async function fetchCampaign(campaignId: number): Promise<Campaign> {
  return request<Campaign>(`/api/mail/campaigns/${campaignId}`)
}

export async function createCampaign(body: CampaignCreate): Promise<Campaign> {
  return request<Campaign>('/api/mail/campaigns', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/**
 * The per-domain daily send cap is a single GLOBAL setting applied to every
 * campaign (not a per-campaign field). Read/write it from the manager page.
 */
export async function fetchCampaignSettings(): Promise<CampaignSettings> {
  return request<CampaignSettings>('/api/mail/campaigns/settings')
}

export async function updateCampaignSettings(perDomainDaily: number): Promise<CampaignSettings> {
  return request<CampaignSettings>('/api/mail/campaigns/settings', {
    method: 'PUT',
    body: JSON.stringify({ per_domain_daily: perDomainDaily }),
  })
}

/** Launch (or resume) a campaign. Over the size threshold: 409 confirm flow. */
export async function startCampaign(
  campaignId: number,
  opts: { confirm?: boolean; confirmToken?: string } = {},
): Promise<Campaign> {
  return request<Campaign>(
    `/api/mail/campaigns/${campaignId}/start${confirmQuery(opts)}`,
    { method: 'POST' },
  )
}

export async function pauseCampaign(campaignId: number): Promise<Campaign> {
  return request<Campaign>(`/api/mail/campaigns/${campaignId}/pause`, { method: 'POST' })
}

export async function resumeCampaign(
  campaignId: number,
  opts: { confirm?: boolean; confirmToken?: string } = {},
): Promise<Campaign> {
  return request<Campaign>(
    `/api/mail/campaigns/${campaignId}/resume${confirmQuery(opts)}`,
    { method: 'POST' },
  )
}

export async function cancelCampaign(campaignId: number): Promise<Campaign> {
  return request<Campaign>(`/api/mail/campaigns/${campaignId}/cancel`, { method: 'POST' })
}

/**
 * Continue a campaign by appending another search's recipients. The backend
 * re-runs dedupe + suppression and gates on the campaign's cumulative size
 * (409 confirm flow) — so the confirm token is threaded the same way as start.
 */
export async function addCampaignSource(
  campaignId: number,
  body: CampaignSourceIn,
  opts: { confirm?: boolean; confirmToken?: string } = {},
): Promise<SourceMerge> {
  return request<SourceMerge>(
    `/api/mail/campaigns/${campaignId}/sources${confirmQuery(opts)}`,
    { method: 'POST', body: JSON.stringify(body) },
  )
}

function confirmQuery(opts: { confirm?: boolean; confirmToken?: string }): string {
  const search = new URLSearchParams()
  if (opts.confirm) search.set('confirm', 'true')
  if (opts.confirmToken) search.set('confirm_token', opts.confirmToken)
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

// ---------------------------------------------------------------------------
// Saved searches — the source lists a campaign is built from.
//
// These live in the search backend (search history), reached same-origin at
// /api/search/*. The shared Keycloak token already carries the search audience
// (the hub's search app uses it), so this is a plain read; recipients are then
// handed to mail's campaign endpoints. No search backend contract is changed.
// ---------------------------------------------------------------------------

export async function fetchSavedSearches(username?: string): Promise<SavedSearch[]> {
  const search = new URLSearchParams()
  if (username) search.set('username', username)
  const qs = search.toString()
  return request<SavedSearch[]>(`/api/search/searches${qs ? `?${qs}` : ''}`)
}

/**
 * Pull a saved search's full recipient list via its CSV export (name,email),
 * shaping it into campaign recipients. Rows with no usable email (the backend
 * writes the literal "null") and the truncation sentinel row are dropped.
 */
export async function fetchSearchRecipients(
  searchId: number,
): Promise<CampaignSourceIn['recipients']> {
  const token = await getAccessToken()
  if (!token) {
    if (hasSession()) {
      const message = 'Could not refresh the session — retry shortly'
      throw new ApiError(503, message, message)
    }
    redirectToLogin()
    throw new ApiError(401, 'Unauthorized', 'Unauthorized')
  }
  const response = await fetch(`/api/search/searches/${searchId}/export.csv`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (response.status === 401) {
    clearTokens()
    redirectToLogin()
    throw new ApiError(401, 'Unauthorized', 'Unauthorized')
  }
  if (!response.ok) {
    throw new ApiError(response.status, 'Export failed', `Could not read search list (${response.status})`)
  }
  const text = await response.text()
  return parseRecipientCsv(text)
}

/** Minimal RFC-4180 CSV parse of the `name,email` export into recipients. */
function parseRecipientCsv(text: string): CampaignSourceIn['recipients'] {
  const rows = parseCsvRows(text)
  const out: NonNullable<CampaignSourceIn['recipients']> = []
  const seen = new Set<string>()
  for (let i = 0; i < rows.length; i++) {
    const [nameCell, emailCell] = rows[i]
    if (i === 0 && emailCell?.trim().toLowerCase() === 'email') continue // header
    const name = (nameCell ?? '').trim()
    const email = (emailCell ?? '').trim()
    if (name.startsWith('ERROR:')) continue // truncation sentinel
    if (!email || email.toLowerCase() === 'null' || !email.includes('@')) continue
    const key = email.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ email, name: name === 'null' ? '' : name })
  }
  return out
}

function parseCsvRows(text: string): string[][] {
  const rows: string[][] = []
  let field = ''
  let row: string[] = []
  let inQuotes = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        field += ch
      }
      continue
    }
    if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      row.push(field)
      field = ''
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(field)
      rows.push(row)
      field = ''
      row = []
    } else {
      field += ch
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows
}
