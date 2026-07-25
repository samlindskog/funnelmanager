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

export interface TaskDetail extends TaskSummary {
  params: Record<string, unknown>
  result: string | null
  error: string | null
  steps: number
  usage: Record<string, unknown> | null
}

export interface TaskListResponse {
  tasks: TaskSummary[]
}

export interface CreateTaskRequest {
  goal: string
  params?: Record<string, unknown>
}
