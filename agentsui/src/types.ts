/** Type contracts mirroring the agents **sessions** API (agents/app/schemas.py).
 *
 * A session is one persistent, multi-turn conversation with a runtime AI agent.
 * Sessions are cross-user VISIBLE (principle 1): the list/detail views surface
 * every session's owner/origin/actor for attribution but never hide another
 * user's session. The one per-user carve-out is destructive ownership
 * (rename/delete/post-your-own), enforced server-side as a 404/403 — not a read
 * filter. */

export interface User {
  username: string
}

/** Derived session status (running/paused/scheduled/error/idle) — computed
 * server-side from live turn state, never stored. */
export type SessionStatus = 'running' | 'paused' | 'scheduled' | 'error' | 'idle'

export interface SessionSummary {
  id: string
  title: string
  model: string
  owner: string
  origin: string
  actor: string
  status: string
  created_at: string
  updated_at: string
}

/** One verbatim transcript message. `content` is the serialized pydantic-ai
 * ModelMessage (the source of truth); `kind` is a coarse display label. */
export interface MessageOut {
  id: string
  seq: number
  turn_id: string
  kind: string
  content: Record<string, unknown>
  model: string | null
  usage: UsageStat | null
  created_at: string
}

/** A Principle-4 pending human approval blocking a paused turn. An
 * over-threshold action the runtime agent attempted but cannot self-confirm —
 * the turn is paused until the initiating human (the session `owner`, acting as a
 * real person) approves or rejects. Read cross-user (principle 1); only the owner
 * may decide (enforced server-side). Mirrors the backend `PendingApprovalOut`. */
export interface PendingApproval {
  id: string
  session_id: string
  // Persisted-only attribution fields — present on the backend `PendingApprovalOut`
  // but never read by the UI, so optional (a live approval omits them).
  turn_id?: string
  subject?: string
  approval_ref?: string
  action: string
  estimate: number
  threshold: number | null
  unit: string
  message: string
  tool_name: string
  status: string
  created_at: string
}

export interface ScheduleOut {
  id: string
  session_id: string
  owner: string
  origin: string
  actor: string
  spec: Record<string, unknown>
  prompt: string
  status: string
  next_run_at: string | null
  last_run_at: string | null
  created_at: string
}

export interface SessionDetail extends SessionSummary {
  context_watermark: number
  last_error: string | null
  messages: MessageOut[]
  pending_approvals: PendingApproval[]
  schedules: ScheduleOut[]
}

export interface SessionListResponse {
  sessions: SessionSummary[]
  /** Distinct owners across ALL sessions (for the cross-user owner picker). */
  owners: string[]
}

export interface CreateSessionRequest {
  title?: string
  model?: string
}

export interface ModelInfo {
  id: string
  context_window: number
  owned_by: string
}

export interface ModelsResponse {
  models: ModelInfo[]
  default: string
}

/** Per-response / per-turn token usage (a serialized pydantic-ai RunUsage). */
export interface UsageStat {
  requests?: number
  tool_calls?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cache_read_tokens?: number
  cost?: number | null
}

export interface ModelUsageStat {
  model: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  requests: number
  tool_calls: number
  turns: number
  cost: number | null
}

export interface StatsResponse {
  by_model: ModelUsageStat[]
  total_cost: number | null
}

export type ApprovalDecision = 'approve' | 'reject'

export interface ApprovalDecisionResponse {
  approval: PendingApproval
  resumed: boolean
}

// --- NDJSON turn-stream events (agents/app/runner.py) -----------------------
// The browser turn stream. An in-stream `error` is a transcript error line —
// NEVER fatal to the app (P8). `idle` is only emitted by the reattach endpoint
// when no turn is buffered for a session.

export type TurnEvent =
  | { type: 'message_start'; role: string; text: string }
  | { type: 'text_delta'; text: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; id: string; name: string; content: unknown }
  | {
      type: 'approval_required'
      approval_id: string
      action: string
      estimate: number
      threshold: number | null
      unit: string
      message: string
      tool_name: string
    }
  | { type: 'summary'; text: string }
  | ({ type: 'usage' } & UsageStat)
  | { type: 'turn_complete'; usage: UsageStat }
  | { type: 'error'; detail: string }
  | { type: 'idle' }
  | { type: string; [key: string]: unknown }
