import { clearTokens, config, getAccessToken, hasSession } from './oidc'
import type {
  ApolloRecord,
  AppLink,
  EntityType,
  HistoryOwner,
  SearchHistoryDetail,
  SearchHistorySummary,
  SearchResponse,
} from './types'

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
  headers.set('Authorization', `Bearer ${await bearerToken()}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })
  // A 401 on an authenticated request means the session expired/was revoked —
  // reset auth state and route back to login.
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
    throw new ApiError(response.status, detail, detailMessage(detail, `Request failed (${response.status})`))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Hub apps — static per-environment config (identity lives in Keycloak now,
// so there is no auth service to serve this list)
// ---------------------------------------------------------------------------

const DEFAULT_APPS: AppLink[] = [
  { name: 'Search', description: 'Apollo person/company search', url: '/search', probe: '/api/search/whoami' },
  { name: 'Mail', description: 'Google Workspace inboxes & sending', url: '/mail/', probe: '/api/mail/whoami' },
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
  headers.set('Authorization', `Bearer ${await bearerToken()}`)
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

/** Distinct search-history owners with counts (populates the owner filter). */
export async function listHistoryOwners(): Promise<HistoryOwner[]> {
  return request<HistoryOwner[]>('/api/search/users')
}

/** Search history, cross-user visible. Pass ``username`` to scope to one owner;
 * omit for all users. */
export async function listSearches(username?: string): Promise<SearchHistorySummary[]> {
  const path =
    username != null && username !== ''
      ? `/api/search/searches?username=${encodeURIComponent(username)}`
      : '/api/search/searches'
  return request<SearchHistorySummary[]>(path)
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
  headers.set('Authorization', `Bearer ${await bearerToken()}`)
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
