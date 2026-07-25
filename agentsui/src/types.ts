export interface User {
  username: string
  role: string
}

/** A runtime-AI-agent run. Cross-user visible (principle 1): the list/detail
 * views surface every run's owner/origin/actor for attribution but never hide
 * another user's run. Mirrors the agents backend `TaskSummary`/`TaskDetail`. */
export interface TaskSummary {
  id: string
  goal: string
  status: string
  owner: string
  origin: string
  actor: string
  progress: number | null
  created_at: string
  started_at: string | null
  ended_at: string | null
}

/** A Principle-4 pending human approval blocking a run (agent hard-enforcement).
 * An over-threshold action the runtime agent attempted but cannot self-confirm —
 * it is paused until the initiating human approves or rejects via
 * POST /api/agents/tasks/{id}/approvals/{aid}. Read cross-user (principle 1) but
 * only the run's `owner` may decide (enforced server-side). Mirrors the agents
 * backend `PendingApprovalOut`. */
export interface PendingApproval {
  id: string
  run_id: string
  subject: string
  approval_ref: string
  action: string
  estimate: number
  threshold: number | null
  unit: string
  message: string
  tool_name: string
  status: string
  created_at: string
}

export interface TaskDetail extends TaskSummary {
  params: Record<string, unknown>
  result: string | null
  error: string | null
  steps: number
  usage: Record<string, unknown> | null
  /** Actionable Principle-4 approvals blocking this run (empty when none). */
  pending_approvals: PendingApproval[]
}

export type ApprovalDecision = 'approve' | 'reject'

/** Outcome of an approve/reject. Mirrors the backend `ApprovalDecisionResponse`;
 * note the minted approval token is NEVER returned — it stays server-side. */
export interface ApprovalDecisionResponse {
  approval: PendingApproval
  run_status: string
  resumed: boolean
}

export interface TaskListResponse {
  tasks: TaskSummary[]
}

export interface CreateTaskRequest {
  goal: string
  params?: Record<string, unknown>
}
