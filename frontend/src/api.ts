import type {
  AccountRequest,
  ApolloRecord,
  AppLink,
  ChannelRequest,
  EntityType,
  Grant,
  Role,
  SearchHistoryDetail,
  SearchHistorySummary,
  SearchResponse,
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

/** Approve the OpenClaw DM pairing for a pending channel request (the sender
 * can then talk to the agent; profile assignment stays a separate step). */
export async function approveChannelPairing(
  channel: string,
  deviceId: string,
): Promise<ChannelRequest & { paired: boolean; linked: boolean }> {
  return request<ChannelRequest & { paired: boolean; linked: boolean }>(
    '/api/auth/admin/channel-requests/approve-pairing',
    {
      method: 'POST',
      body: JSON.stringify({ channel, device_id: deviceId }),
    },
  )
}

// ---------------------------------------------------------------------------
// Search app — Apollo searches, enrichment, NDJSON streams (search backend)
// ---------------------------------------------------------------------------

export type RunSearchParams = {
  query: string
  entity_type: EntityType
  page?: number
  per_page?: number
  organization_id?: string
  organization_name?: string
  organization_display_name?: string
  organization_domain?: string
  company_name?: string
  company_domain?: string
}

export type SearchProgress = {
  kind: 'ingest'
  page: number
  total_pages: number
  stored: number
  /** Mongo `_id`s from the latest ingest page (search + enrich streams). */
  ids?: string[]
  ingest_stream_id?: string
  embedding_stream_id?: string
  /** All in-flight leads stream jobs (enrich batch); cancel should hit every id. */
  active_stream_ids?: string[]
  /** In-flight ingest stream jobs only; cancelling fetching must not touch embedding. */
  active_ingest_stream_ids?: string[]
  /** Match stream: people still waiting on async waterfall/phone webhook. */
  phone_reveal_pending_count?: number
  waterfall_pending_count?: number
  phone_reveal_pending?: boolean
  waterfall_pending?: boolean
}

export type EmbeddingProgress = {
  kind: 'embedding'
  done: number
  total: number
  complete?: boolean
  error?: string
  /** True while a batch is running but not yet counted in ``done``. */
  in_flight?: boolean
  embedding_stream_id?: string
  active_stream_ids?: string[]
  /** In-flight embedding stream jobs only; cancelling embedding must not touch ingest. */
  active_embedding_stream_ids?: string[]
  phone_reveal_pending_count?: number
  waterfall_pending_count?: number
}

export type RunSearchHandlers = {
  onProgress?: (progress: SearchProgress) => void
  onEmbeddingProgress?: (progress: EmbeddingProgress) => void
  /** Fired as soon as page 1 is hydrated; further UI pages use sync fetchSearchPage. */
  onFirstPage?: (response: SearchResponse) => void
  /** Fired when Apollo ingest finishes (embedding may still be running). */
  onComplete?: (response: SearchResponse) => void
}

function asSearchResponse(event: Record<string, unknown>): SearchResponse {
  const { type: _type, ...rest } = event
  return rest as unknown as SearchResponse
}

export async function runSearch(
  params: RunSearchParams,
  handlers: RunSearchHandlers | ((progress: SearchProgress) => void) = {},
): Promise<SearchResponse> {
  const { onProgress, onEmbeddingProgress, onFirstPage, onComplete } =
    typeof handlers === 'function' ? { onProgress: handlers } : handlers

  const headers = new Headers()
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'application/x-ndjson')

  const response = await fetch('/api/search/search', {
    method: 'POST',
    headers,
    body: JSON.stringify(params),
  })
  if (response.status === 401) {
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
    throw new ApiError(
      response.status,
      detail,
      detailMessage(detail, `Request failed (${response.status})`),
    )
  }

  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/x-ndjson')) {
    return (await response.json()) as SearchResponse
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new ApiError(502, 'Empty stream', 'Search stream returned no body')
  }

  const decoder = new TextDecoder()
  let buffer = ''
  let complete: SearchResponse | null = null
  let firstPage: SearchResponse | null = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    while (true) {
      const newline = buffer.indexOf('\n')
      if (newline < 0) break
      const line = buffer.slice(0, newline).trim()
      buffer = buffer.slice(newline + 1)
      if (!line) continue
      let event: Record<string, unknown>
      try {
        event = JSON.parse(line) as Record<string, unknown>
      } catch {
        continue
      }
      if (event.type === 'progress') {
        const mongoIds = Array.isArray(event.ids)
          ? event.ids.map((id) => String(id).trim()).filter(Boolean)
          : []
        onProgress?.({
          kind: 'ingest',
          page: Number(event.page) || 0,
          total_pages: Number(event.total_pages) || 0,
          stored: Number(event.stored) || 0,
          ids: mongoIds,
          ingest_stream_id:
            typeof event.ingest_stream_id === 'string' ? event.ingest_stream_id : undefined,
          embedding_stream_id:
            typeof event.embedding_stream_id === 'string'
              ? event.embedding_stream_id
              : undefined,
        })
      } else if (event.type === 'embedding_progress') {
        onEmbeddingProgress?.({
          kind: 'embedding',
          done: Number(event.done) || 0,
          total: Number(event.total) || 0,
          complete: Boolean(event.complete),
          error: typeof event.error === 'string' ? event.error : undefined,
          embedding_stream_id:
            typeof event.embedding_stream_id === 'string'
              ? event.embedding_stream_id
              : undefined,
        })
      } else if (event.type === 'first_page') {
        firstPage = asSearchResponse(event)
        onFirstPage?.(firstPage)
      } else if (event.type === 'complete') {
        complete = asSearchResponse(event)
        onComplete?.(complete)
      } else if (event.type === 'error') {
        const detail = event.detail ?? 'Search failed'
        throw new ApiError(502, detail, detailMessage(detail, 'Search failed'))
      }
    }
  }

  if (complete) return complete
  if (firstPage) return firstPage
  throw new ApiError(502, 'Incomplete stream', 'Search stream ended without a complete event')
}

export async function listSearches(): Promise<SearchHistorySummary[]> {
  return request<SearchHistorySummary[]>('/api/search/searches')
}

export async function getSearch(id: number): Promise<SearchHistoryDetail> {
  return request<SearchHistoryDetail>(`/api/search/searches/${id}`)
}

export async function cancelStream(streamId: string): Promise<{ stream_id: string; cancelled: boolean }> {
  return request<{ stream_id: string; cancelled: boolean }>(
    `/api/search/streams/${encodeURIComponent(streamId)}/cancel`,
    { method: 'POST' },
  )
}

export async function fetchSearchPage(id: number, page: number): Promise<SearchResponse> {
  return request<SearchResponse>(`/api/search/searches/${id}/page`, {
    method: 'POST',
    body: JSON.stringify({ page }),
  })
}

export async function deleteSearch(id: number): Promise<void> {
  await request<void>(`/api/search/searches/${id}`, { method: 'DELETE' })
}

export type Lead = {
  id: string
  apollo_id: string
  entity_type: 'person' | 'organization'
  apollo_enriched: {
    linkedin: boolean
    email: boolean
    phone: boolean
  }
  apollo_responses: Record<
    string,
    {
      received_at: string
      data: Record<string, unknown>
    }
  >
  created_at: string
  updated_at: string
}

export async function getPersonLead(mongoId: string): Promise<ApolloRecord> {
  return request<ApolloRecord>(`/api/search/leads/${encodeURIComponent(mongoId)}`)
}

export async function enrichLead(
  apolloPersonId?: string,
  params: Record<string, unknown> = {},
): Promise<ApolloRecord> {
  // Search backend hydrates leads SearchIdsOut into a UI record.
  const path =
    apolloPersonId != null && apolloPersonId !== ''
      ? `/api/search/people/enrich/${encodeURIComponent(apolloPersonId)}`
      : '/api/search/people/enrich'
  return request<ApolloRecord>(path, {
    method: 'POST',
    body: JSON.stringify(params),
  })
}

export type EnrichStreamHandlers = {
  onProgress?: (progress: SearchProgress) => void
  onEmbeddingProgress?: (progress: EmbeddingProgress) => void
  /** Called with mongo `_id`s from each progress event (hydrate via getPersonLead). */
  onIds?: (mongoIds: string[]) => void
  onComplete?: () => void
  /** Abort cancels the browser-side NDJSON read (pair with cancelStream on leads). */
  signal?: AbortSignal
}

async function consumePeopleProgressStream(
  path: string,
  body: Record<string, unknown>,
  handlers: EnrichStreamHandlers,
  errorLabel: string,
): Promise<void> {
  const { onProgress, onEmbeddingProgress, onIds, onComplete, signal } = handlers
  if (signal?.aborted) return

  const headers = new Headers()
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'application/x-ndjson')

  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if (signal?.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
      return
    }
    throw err
  }
  if (response.status === 401) {
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
    throw new ApiError(
      response.status,
      detail,
      detailMessage(detail, `Request failed (${response.status})`),
    )
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new ApiError(502, 'Empty stream', `${errorLabel} stream returned no body`)
  }

  const onAbort = () => {
    void reader.cancel()
  }
  signal?.addEventListener('abort', onAbort, { once: true })

  const decoder = new TextDecoder()
  let buffer = ''
  let completed = false

  try {
    while (true) {
      if (signal?.aborted) break
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      while (true) {
        const newline = buffer.indexOf('\n')
        if (newline < 0) break
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (!line) continue
        let event: Record<string, unknown>
        try {
          event = JSON.parse(line) as Record<string, unknown>
        } catch {
          continue
        }
        if (signal?.aborted) break
        if (event.type === 'progress') {
          const mongoIds = Array.isArray(event.ids)
            ? event.ids.map((id) => String(id).trim()).filter(Boolean)
            : []
          const activeIds = Array.isArray(event.active_stream_ids)
            ? event.active_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          const activeIngestIds = Array.isArray(event.active_ingest_stream_ids)
            ? event.active_ingest_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          onProgress?.({
            kind: 'ingest',
            page: Number(event.page) || 0,
            total_pages: Number(event.total_pages) || 0,
            stored: Number(event.stored) || 0,
            ids: mongoIds,
            ingest_stream_id:
              typeof event.ingest_stream_id === 'string' ? event.ingest_stream_id : undefined,
            embedding_stream_id:
              typeof event.embedding_stream_id === 'string'
                ? event.embedding_stream_id
                : undefined,
            active_stream_ids: activeIds,
            active_ingest_stream_ids: activeIngestIds,
            phone_reveal_pending_count:
              typeof event.phone_reveal_pending_count === 'number'
                ? event.phone_reveal_pending_count
                : undefined,
            waterfall_pending_count:
              typeof event.waterfall_pending_count === 'number'
                ? event.waterfall_pending_count
                : undefined,
            phone_reveal_pending: Boolean(event.phone_reveal_pending),
            waterfall_pending: Boolean(event.waterfall_pending),
          })
          if (mongoIds.length) onIds?.(mongoIds)
        } else if (event.type === 'embedding_progress') {
          const activeIds = Array.isArray(event.active_stream_ids)
            ? event.active_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          const activeEmbeddingIds = Array.isArray(event.active_embedding_stream_ids)
            ? event.active_embedding_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          onEmbeddingProgress?.({
            kind: 'embedding',
            done: Number(event.done) || 0,
            total: Number(event.total) || 0,
            complete: Boolean(event.complete),
            error: typeof event.error === 'string' ? event.error : undefined,
            in_flight: Boolean(event.in_flight),
            embedding_stream_id:
              typeof event.embedding_stream_id === 'string'
                ? event.embedding_stream_id
                : undefined,
            active_stream_ids: activeIds,
            active_embedding_stream_ids: activeEmbeddingIds,
            phone_reveal_pending_count:
              typeof event.phone_reveal_pending_count === 'number'
                ? event.phone_reveal_pending_count
                : undefined,
            waterfall_pending_count:
              typeof event.waterfall_pending_count === 'number'
                ? event.waterfall_pending_count
                : undefined,
          })
        } else if (event.type === 'ingest_complete') {
          // Clear/update the fetch circle when ingest finishes (embed may continue).
          const activeIds = Array.isArray(event.active_stream_ids)
            ? event.active_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          const activeIngestIds = Array.isArray(event.active_ingest_stream_ids)
            ? event.active_ingest_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          onProgress?.({
            kind: 'ingest',
            page: Number(event.page) || Number(event.stored) || 0,
            total_pages: Number(event.total_pages) || 0,
            stored: Number(event.stored) || 0,
            ingest_stream_id:
              typeof event.ingest_stream_id === 'string' ? event.ingest_stream_id : undefined,
            embedding_stream_id:
              typeof event.embedding_stream_id === 'string'
                ? event.embedding_stream_id
                : undefined,
            active_stream_ids: activeIds,
            active_ingest_stream_ids: activeIngestIds,
            // Reuse total_pages<=0 clear path when the whole batch ingest is done.
            ...(event.complete ? { page: 0, total_pages: 0, stored: Number(event.stored) || 0 } : {}),
          })
        } else if (event.type === 'complete') {
          completed = true
          onComplete?.()
        } else if (event.type === 'error') {
          const detail = event.detail ?? `${errorLabel} failed`
          throw new ApiError(502, detail, detailMessage(detail, `${errorLabel} failed`))
        }
      }
    }
  } catch (err) {
    if (signal?.aborted || (err instanceof DOMException && err.name === 'AbortError')) {
      return
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
  }

  if (!completed && !signal?.aborted) {
    onComplete?.()
  }
}

/** Stream LinkedIn/complete-profile enrich (same NDJSON events as search). */
export async function enrichPeopleStream(
  apolloIds: string[],
  handlers: EnrichStreamHandlers = {},
): Promise<void> {
  const ids = [...new Set(apolloIds.map((id) => id.trim()).filter(Boolean))]
  if (!ids.length) return
  await consumePeopleProgressStream(
    '/api/search/people/enrich',
    { ids, stream: true },
    handlers,
    'Enrich',
  )
}

export type MatchStreamParams = {
  run_waterfall_email?: boolean
  run_waterfall_phone?: boolean
  reveal_phone_number?: boolean
}

/** Stream people/match (email/phone waterfall) with the same NDJSON progress as enrich. */
export async function matchPeopleStream(
  apolloIds: string[],
  params: MatchStreamParams = {},
  handlers: EnrichStreamHandlers = {},
): Promise<void> {
  const ids = [...new Set(apolloIds.map((id) => id.trim()).filter(Boolean))]
  if (!ids.length) return
  await consumePeopleProgressStream(
    '/api/search/people/match',
    {
      ids,
      stream: true,
      ...(params.run_waterfall_email ? { run_waterfall_email: true } : {}),
      ...(params.run_waterfall_phone ? { run_waterfall_phone: true } : {}),
      ...(params.reveal_phone_number ? { reveal_phone_number: true } : {}),
    },
    handlers,
    'Match',
  )
}

export type PersonMatchResult = {
  lead: ApolloRecord | null
  phone_reveal_pending: boolean
  waterfall_pending?: boolean
  webhook_url: string | null
  raw_lead?: Lead
}

/** Non-streaming people/match (hydrated lead). Prefer ``matchPeopleStream`` for UI batches. */
export async function matchLead(
  apolloPersonId?: string,
  params: Record<string, unknown> = {},
): Promise<PersonMatchResult> {
  const path =
    apolloPersonId != null && apolloPersonId !== ''
      ? `/api/search/people/match/${encodeURIComponent(apolloPersonId)}`
      : '/api/search/people/match'
  return request<PersonMatchResult>(path, {
    method: 'POST',
    body: JSON.stringify(params),
  })
}

export async function enrichOrganizationLead(
  apolloOrganizationId?: string,
  params: Record<string, unknown> = {},
): Promise<ApolloRecord> {
  const path =
    apolloOrganizationId != null && apolloOrganizationId !== ''
      ? `/api/search/organizations/enrich/${encodeURIComponent(apolloOrganizationId)}`
      : '/api/search/organizations/enrich'
  return request<ApolloRecord>(path, {
    method: 'POST',
    body: JSON.stringify(params),
  })
}

export type ApolloCredits = {
  credits_remaining: number | null
  lead_credits_used: number | null
  effective_lead_credits: number | null
}

export async function getApolloCredits(): Promise<ApolloCredits> {
  return request<ApolloCredits>('/api/search/apollo/credits')
}

export type SimilaritySearchParams = {
  query: string
  limit?: number
}

export type SimilaritySearchResponse = {
  query: string
  results: Array<{ score: number; record: ApolloRecord }>
  history: SearchHistoryDetail
}

export async function runSimilaritySearch(
  params: SimilaritySearchParams,
): Promise<SimilaritySearchResponse> {
  return request<SimilaritySearchResponse>('/api/search/similarity-search', {
    method: 'POST',
    body: JSON.stringify({
      query: params.query,
      limit: params.limit ?? 25,
    }),
  })
}
