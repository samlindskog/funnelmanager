/** Agents sessions API client.
 *
 * The app is served same-origin at /agents/ and shares the hub's Keycloak OIDC
 * session (localStorage fm_oidc_* keys) — whichever app is open refreshes it.
 * There is no login form here: a missing/expired session redirects straight to
 * Keycloak and returns to /agents/ afterwards.
 *
 * Two request shapes: `request<T>` for JSON endpoints, and `streamRequest` for
 * the NDJSON turn stream (`POST /sessions/{id}/messages`, `GET
 * /sessions/{id}/stream`) — the latter returns the raw `Response` so `stream.ts`
 * can read it incrementally. Cross-user visible (principle 1): the session list
 * is everyone's sessions, attributed — never owner-filtered as a security
 * boundary (the `owner` param is a convenience view filter only).
 */

import { beginLogin, clearTokens, getAccessToken, hasSession, logout as oidcLogout } from './oidc'
import type {
  ApprovalDecision,
  ApprovalDecisionResponse,
  CreateSessionRequest,
  ModelsResponse,
  SessionDetail,
  SessionListResponse,
  SessionSummary,
  StatsResponse,
} from './types'

export function redirectToLogin(): void {
  void beginLogin('/agents/')
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

/** Attach the bearer token; handle the no-token / 401 auth lifecycle uniformly.
 * Returns the raw `Response` (used by both JSON and streaming callers). Throws an
 * `ApiError` for a non-2xx response after draining the JSON error detail — so a
 * streaming caller sees a 409 (turn active) / 403 (not owner) BEFORE it starts
 * reading the body. */
async function send(path: string, init: RequestInit = {}): Promise<Response> {
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
  return response
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await send(path, init)
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
// Sessions (interactive multi-turn agent chat). Cross-user visible (principle 1).
// ---------------------------------------------------------------------------

export type SessionListParams = {
  owner?: string
  limit?: number
  offset?: number
}

export async function fetchSessions(params: SessionListParams = {}): Promise<SessionListResponse> {
  const search = new URLSearchParams()
  if (params.owner) search.set('owner', params.owner)
  search.set('limit', String(params.limit ?? 50))
  if (params.offset) search.set('offset', String(params.offset))
  return request<SessionListResponse>(`/api/agents/sessions?${search.toString()}`)
}

export async function fetchSession(sessionId: string): Promise<SessionDetail> {
  return request<SessionDetail>(`/api/agents/sessions/${encodeURIComponent(sessionId)}`)
}

export async function createSession(body: CreateSessionRequest): Promise<SessionDetail> {
  return request<SessionDetail>('/api/agents/sessions', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** Rename a session. Owner-only server-side (destructive-ownership carve-out);
 * another user's row 404s. */
export async function renameSession(sessionId: string, title: string): Promise<SessionSummary> {
  return request<SessionSummary>(`/api/agents/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

/** Delete a session (owner-only; another user's row 404s). */
export async function deleteSession(sessionId: string): Promise<void> {
  await request<void>(`/api/agents/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

/** Send a user message and return the raw NDJSON turn-stream `Response` for
 * incremental reading. Owner-only (a serial, single-actor chat); a second
 * concurrent turn is 409, a non-owner poster is 403 — both surface as an
 * `ApiError` before the stream starts. */
export async function postMessage(sessionId: string, content: string, signal?: AbortSignal): Promise<Response> {
  return send(`/api/agents/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
    signal,
  })
}

/** Reattach to a session's most-recent turn (replay buffer -> live), or a single
 * `{type:"idle"}` line if nothing is buffered. Returns the raw NDJSON `Response`. */
export async function reattachStream(sessionId: string, signal?: AbortSignal): Promise<Response> {
  return send(`/api/agents/sessions/${encodeURIComponent(sessionId)}/stream`, { signal })
}

/** Approve or reject a Principle-4 pending approval blocking a paused turn.
 *
 * Only the initiating human (the session owner, acting as a real person — never
 * via an agent) may decide; the server enforces this and rejects everyone else.
 * On `approve` it mints a human-authorized token server-side and resumes the turn
 * so it re-issues the SAME over-threshold action; on `reject` that action is
 * skipped. The token is never exposed to this client. */
export async function decideApproval(
  sessionId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<ApprovalDecisionResponse> {
  return request<ApprovalDecisionResponse>(
    `/api/agents/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}`,
    {
      method: 'POST',
      body: JSON.stringify({ decision }),
    },
  )
}

// ---------------------------------------------------------------------------
// Models + usage stats
// ---------------------------------------------------------------------------

export async function fetchModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/api/agents/models')
}

export async function fetchStats(owner?: string): Promise<StatsResponse> {
  const search = new URLSearchParams()
  if (owner) search.set('owner', owner)
  const query = search.toString()
  return request<StatsResponse>(`/api/agents/stats${query ? `?${query}` : ''}`)
}
