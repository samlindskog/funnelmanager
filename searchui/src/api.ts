import { beginLogin, clearTokens, getAccessToken, hasSession } from './oidc'
import type {
  ApolloRecord,
  EntityType,
  HistoryOwner,
  SearchHistoryDetail,
  SearchHistorySummary,
  SearchResponse,
} from './types'

/** No login form here: the search app is served same-origin at /search/ and
 * shares the hub's Keycloak session. A missing/expired session redirects
 * straight to Keycloak and returns to /search/ afterwards. */
export function redirectToLogin(): void {
  void beginLogin('/search/')
}

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
  /** ingest_complete may report the run stopped short (Apollo page cap / max entries). */
  partial?: boolean
  partial_reason?: string
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
  /** Rows actually indexed into Milvus so far (honest count; `done` counts attempts). */
  indexed?: number
  /** Rows that failed to embed/index so far. On the terminal `complete` event,
   * `total` is ATTEMPTED, so `done` < `total` legitimately when `failed` > 0. */
  failed?: number
  /** Set when this update came from an `item_error` line (a per-chunk enrich
   * failure) rather than a progress/complete line — non-terminal; a `complete`
   * still follows. Carries a human-readable `error`/detail. */
  item_error?: boolean
  embedding_stream_id?: string
  active_stream_ids?: string[]
  /** In-flight embedding stream jobs only; cancelling embedding must not touch ingest. */
  active_embedding_stream_ids?: string[]
  phone_reveal_pending_count?: number
  waterfall_pending_count?: number
}

/** Backpressure notice: Apollo ingest is deliberately paused while embedding
 * catches up. Non-terminal and informational — expected, healthy behavior. */
export type ThrottleEvent = {
  kind: 'ingest'
  reason?: string | null
  queue_pages: number
  waited_s: number
  stored: number
  ingest_stream_id?: string
  embedding_stream_id?: string
}

function parseThrottle(event: Record<string, unknown>): ThrottleEvent {
  return {
    kind: 'ingest',
    reason: typeof event.reason === 'string' ? event.reason : null,
    queue_pages: Number(event.queue_pages) || 0,
    waited_s: Number(event.waited_s) || 0,
    stored: Number(event.stored) || 0,
    ingest_stream_id:
      typeof event.ingest_stream_id === 'string' ? event.ingest_stream_id : undefined,
    embedding_stream_id:
      typeof event.embedding_stream_id === 'string' ? event.embedding_stream_id : undefined,
  }
}

/** Parse an `embedding_progress` / embedding `item_error` line into EmbeddingProgress. */
function parseEmbedding(
  event: Record<string, unknown>,
  extra: Partial<EmbeddingProgress> = {},
): EmbeddingProgress {
  return {
    kind: 'embedding',
    done: Number(event.done) || 0,
    total: Number(event.total) || 0,
    complete: Boolean(event.complete),
    error: typeof event.error === 'string' ? event.error : undefined,
    indexed: typeof event.indexed === 'number' ? event.indexed : undefined,
    failed: typeof event.failed === 'number' ? event.failed : undefined,
    embedding_stream_id:
      typeof event.embedding_stream_id === 'string' ? event.embedding_stream_id : undefined,
    ...extra,
  }
}

export type RunSearchHandlers = {
  onProgress?: (progress: SearchProgress) => void
  onEmbeddingProgress?: (progress: EmbeddingProgress) => void
  /** Fired while ingest is paused waiting for embedding to catch up (backpressure). */
  onThrottled?: (event: ThrottleEvent) => void
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
  const { onProgress, onEmbeddingProgress, onThrottled, onFirstPage, onComplete } =
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
        onEmbeddingProgress?.(parseEmbedding(event))
      } else if (event.type === 'throttled') {
        onThrottled?.(parseThrottle(event))
      } else if (event.type === 'item_error' && event.kind === 'embedding') {
        // Per-chunk embedding failure — non-terminal; a `complete` still follows.
        onEmbeddingProgress?.(parseEmbedding(event, { complete: false, item_error: true }))
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
  /** Fired while ingest is paused waiting for embedding to catch up (backpressure). */
  onThrottled?: (event: ThrottleEvent) => void
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
  const { onProgress, onEmbeddingProgress, onThrottled, onIds, onComplete, signal } = handlers
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
        } else if (event.type === 'embedding_progress' || event.type === 'item_error') {
          // `item_error` with kind:"embedding" is a non-terminal per-chunk failure;
          // it carries the same done/total/indexed/failed shape (a `complete` follows).
          if (event.type === 'item_error' && event.kind !== 'embedding') continue
          const activeIds = Array.isArray(event.active_stream_ids)
            ? event.active_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          const activeEmbeddingIds = Array.isArray(event.active_embedding_stream_ids)
            ? event.active_embedding_stream_ids.map((id) => String(id).trim()).filter(Boolean)
            : undefined
          onEmbeddingProgress?.(
            parseEmbedding(event, {
              complete: event.type === 'item_error' ? false : Boolean(event.complete),
              item_error: event.type === 'item_error',
              in_flight: Boolean(event.in_flight),
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
            }),
          )
        } else if (event.type === 'throttled') {
          onThrottled?.(parseThrottle(event))
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
            // Per-person ingest_complete may report the walk was capped short.
            partial: event.partial ? true : undefined,
            partial_reason: typeof event.reason === 'string' ? event.reason : undefined,
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

/** Per-kind embeddings the similarity search may rank by (leads v2 contract). */
export type EmbedKind = 'apollo' | 'name' | 'title'

export type SimilaritySearchParams = {
  query: string
  limit?: number
  /** Which per-kind embeddings to rank by. `[]` = pure filter search. Always
   * sent explicitly by the UI (the server's omitted-param legacy default —
   * `["apollo"]` — never applies here). */
  embeds: EmbedKind[]
  /** Mongo `_id` of a stored organization doc; filters people by company. */
  companyId?: string
  /** Multi-company OR filter — each entry an Apollo org id or company record id. */
  companyIds?: string[]
  /** Tri-state exists filters (true=has, false=missing, undefined=no filter). */
  emailExists?: boolean
  phoneExists?: boolean
  linkedinExists?: boolean
}

export type SimilaritySearchResponse = {
  query: string
  // Pure filter search (embeds == []) returns null scores (no vector ranking).
  results: Array<{ score: number | null; record: ApolloRecord }>
  history: SearchHistoryDetail
}

/** Download the FULL stored result list of a search as CSV (server-streamed). */
export async function downloadSearchCsv(searchId: number): Promise<void> {
  const headers = new Headers()
  headers.set('Authorization', `Bearer ${await bearerToken()}`)
  const response = await fetch(`/api/search/searches/${searchId}/export.csv`, { headers })
  if (!response.ok) throw new Error(`Export failed (${response.status})`)
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `search-${searchId}-all.csv`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** Create a search from lead Mongo _ids (CSV re-import flow). */
export async function importSearchFromIds(
  ids: string[],
  label?: string,
): Promise<SimilaritySearchResponse> {
  return request<SimilaritySearchResponse>('/api/search/searches/import', {
    method: 'POST',
    body: JSON.stringify({ ids, label }),
  })
}

export interface CompanyOption {
  mongo_id: string | null
  apollo_id: string | null
  name: string | null
}

/** Recently ingested/updated companies — feeds the company-filter picker. */
export async function listRecentCompanies(limit = 25): Promise<CompanyOption[]> {
  return request<CompanyOption[]>(`/api/search/companies/recent?limit=${limit}`)
}

/** Resolve a company record id or Apollo org id to a named option (404 if unknown). */
export async function resolveCompany(value: string): Promise<CompanyOption> {
  return request<CompanyOption>(`/api/search/companies/resolve?value=${encodeURIComponent(value)}`)
}

export async function runSimilaritySearch(
  params: SimilaritySearchParams,
): Promise<SimilaritySearchResponse> {
  const body: Record<string, unknown> = {
    query: params.query,
    limit: params.limit ?? 25,
    embeds: params.embeds,
  }
  // Map camelCase params to the snake_case leads contract, omitting undefined.
  if (params.companyId != null && params.companyId !== '') {
    body.company_id = params.companyId
  }
  if (params.companyIds && params.companyIds.length > 0) {
    body.company_ids = params.companyIds
  }
  if (params.emailExists !== undefined) body.email_exists = params.emailExists
  if (params.phoneExists !== undefined) body.phone_exists = params.phoneExists
  if (params.linkedinExists !== undefined) body.linkedin_exists = params.linkedinExists
  return request<SimilaritySearchResponse>('/api/search/similarity-search', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
