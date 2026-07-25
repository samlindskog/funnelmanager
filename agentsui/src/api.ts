/** Agents app API client.
 *
 * The app is served same-origin at /agents/ and shares the hub's Keycloak OIDC
 * session (localStorage fm_oidc_* keys) — whichever app is open refreshes it.
 * There is no login form here: a missing/expired session redirects straight
 * to Keycloak and returns to /agents/ afterwards.
 */

import { beginLogin, clearTokens, getAccessToken, hasSession, logout as oidcLogout } from './oidc'
import type {
  ApprovalDecision,
  ApprovalDecisionResponse,
  CreateTaskRequest,
  TaskDetail,
  TaskListResponse,
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
// Agents (runtime-AI-agent runs). Cross-user visible (principle 1): the list
// is everyone's runs, attributed — never owner-filtered as a security boundary.
// ---------------------------------------------------------------------------

export type TaskListParams = {
  owner?: string
  status?: string
  limit?: number
  offset?: number
}

export async function fetchTasks(params: TaskListParams = {}): Promise<TaskListResponse> {
  const search = new URLSearchParams()
  if (params.owner) search.set('owner', params.owner)
  if (params.status) search.set('status', params.status)
  search.set('limit', String(params.limit ?? 50))
  if (params.offset) search.set('offset', String(params.offset))
  const query = search.toString()
  return request<TaskListResponse>(`/api/agents/tasks${query ? `?${query}` : ''}`)
}

export async function fetchTask(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/api/agents/tasks/${encodeURIComponent(taskId)}`)
}

export async function createTask(body: CreateTaskRequest): Promise<TaskDetail> {
  return request<TaskDetail>('/api/agents/tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** Approve or reject a Principle-4 pending approval blocking a paused run.
 *
 * Only the initiating human (the run's owner, acting as a real person — never via
 * an agent) may decide; the server enforces this and rejects everyone else. On
 * `approve` it mints a human-authorized token server-side and resumes the run so
 * it re-issues the SAME over-threshold action; on `reject` that action is skipped.
 * The token is never exposed to this client. */
export async function decideApproval(
  taskId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<ApprovalDecisionResponse> {
  return request<ApprovalDecisionResponse>(
    `/api/agents/tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`,
    {
      method: 'POST',
      body: JSON.stringify({ decision }),
    },
  )
}
