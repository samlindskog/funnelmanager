/** Shared session-status colours + small formatters (kept pure/testable). */

import type { ApiError } from './api'
import type { PendingApproval, SessionSummary } from './types'

export type ChipColor = 'default' | 'info' | 'warning' | 'success' | 'error'

// Derived session statuses (running/paused/scheduled/error/idle).
export const STATUS_COLOR: Record<string, ChipColor> = {
  running: 'info',
  paused: 'warning',
  scheduled: 'info',
  error: 'error',
  idle: 'default',
}

const ACTIVE = new Set(['running', 'paused'])

export function isActive(status: string): boolean {
  return ACTIVE.has(status)
}

export function errorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err && typeof (err as ApiError).message === 'string') {
    return (err as ApiError).message
  }
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}

/** "alice" or "alice (via agent)" — attribution per principle 1/3. */
export function attribution(session: Pick<SessionSummary, 'owner' | 'origin'>): string {
  return session.origin === 'agent' ? `${session.owner} (via agent)` : session.owner
}

export function formatDate(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  return sameDay
    ? date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

/** Trim a resource estimate to a readable number (integers stay whole). */
export function fmtNum(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100)
}

/** "3.4 GB (threshold 2 GB)" — the over-threshold estimate that tripped the gate. */
export function formatEstimate(approval: PendingApproval): string {
  const unit = approval.unit ? ` ${approval.unit}` : ''
  const estimate = `${fmtNum(approval.estimate)}${unit}`
  if (approval.threshold != null) return `${estimate} (threshold ${fmtNum(approval.threshold)}${unit})`
  return estimate
}
