/** Pure transcript view-model logic (unit-testable, no I/O — P11).
 *
 * Two sources feed the same `TranscriptItem[]` render model:
 *   1. persisted history from `GET /sessions/{id}` — each `MessageOut.content` is
 *      a serialized pydantic-ai ModelMessage we flatten into display items;
 *   2. the live NDJSON turn stream — `applyEvent` folds each event in, merging
 *      consecutive text/thinking deltas into the item being streamed.
 * Keeping this pure means the streaming reducer + the history flattener are
 * testable without a browser or a backend. */

import type { MessageOut, PendingApproval, TurnEvent, UsageStat } from './types'

export type TranscriptItem =
  | { key: string; kind: 'user'; text: string }
  | { key: string; kind: 'assistant'; text: string }
  | { key: string; kind: 'thinking'; text: string }
  | { key: string; kind: 'tool_call'; id: string; name: string; args: unknown }
  | { key: string; kind: 'tool_result'; id: string; name: string; content: unknown }
  | { key: string; kind: 'summary'; text: string }
  | { key: string; kind: 'error'; text: string }

// --- serialized ModelMessage flattening ------------------------------------

function partText(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((p) => (typeof p === 'string' ? p : typeof (p as { text?: unknown })?.text === 'string' ? (p as { text: string }).text : ''))
      .join('')
  }
  if (content == null) return ''
  try {
    return JSON.stringify(content)
  } catch {
    return ''
  }
}

/** Flatten one persisted ModelMessage row into zero or more display items. */
function flattenMessage(message: MessageOut): TranscriptItem[] {
  const content = message.content as { kind?: string; parts?: unknown[] }
  const parts = Array.isArray(content?.parts) ? content.parts : []
  const items: TranscriptItem[] = []
  parts.forEach((raw, index) => {
    const part = raw as Record<string, unknown>
    const partKind = String(part.part_kind ?? '')
    const key = `${message.id}:${index}`
    switch (partKind) {
      case 'user-prompt': {
        const text = partText(part.content)
        if (text) items.push({ key, kind: 'user', text })
        break
      }
      case 'text': {
        const text = partText(part.content)
        if (text) items.push({ key, kind: 'assistant', text })
        break
      }
      case 'thinking': {
        const text = partText(part.content)
        if (text) items.push({ key, kind: 'thinking', text })
        break
      }
      case 'tool-call':
        items.push({
          key,
          kind: 'tool_call',
          id: String(part.tool_call_id ?? ''),
          name: String(part.tool_name ?? ''),
          args: part.args ?? {},
        })
        break
      case 'tool-return':
        items.push({
          key,
          kind: 'tool_result',
          id: String(part.tool_call_id ?? ''),
          name: String(part.tool_name ?? ''),
          content: part.content ?? null,
        })
        break
      case 'retry-prompt':
        items.push({
          key,
          kind: 'tool_result',
          id: String(part.tool_call_id ?? ''),
          name: String(part.tool_name ?? 'retry'),
          content: part.content ?? null,
        })
        break
      default:
        break
    }
  })
  // A `summary` row is rendered as a compact notice.
  if (message.kind === 'summary' && items.length > 0) {
    return items.map((item) =>
      item.kind === 'user' ? { key: item.key, kind: 'summary', text: item.text } : item,
    )
  }
  return items
}

export function messagesToTranscript(messages: MessageOut[]): TranscriptItem[] {
  const items: TranscriptItem[] = []
  for (const message of messages) items.push(...flattenMessage(message))
  return items
}

// --- live stream reducer ----------------------------------------------------

/** Fold one live turn event into the transcript, merging consecutive deltas of
 * the same kind into the item being streamed. Returns a new array (never mutates
 * the input) so React re-renders. Unknown / lifecycle-only events
 * (`usage`/`turn_complete`/`idle`/`approval_required`) are no-ops here — usage
 * and approvals are surfaced by the caller through separate state. */
export function applyEvent(items: TranscriptItem[], event: TurnEvent): TranscriptItem[] {
  const nextKey = () => `live-${items.length}`
  const last = items[items.length - 1]

  switch (event.type) {
    case 'message_start': {
      const text = String((event as { text?: unknown }).text ?? '')
      // Defensive dedup: the live stream is the ONLY source of the in-flight
      // user echo (the composer does not optimistically push it), so skip only
      // when the last live item is already this exact user message — guarding
      // against a stream that redundantly replays a user message_start.
      if (last && last.kind === 'user' && last.text === text) return items
      return [...items, { key: nextKey(), kind: 'user', text }]
    }
    case 'text_delta': {
      const delta = String((event as { text?: unknown }).text ?? '')
      if (last && last.kind === 'assistant') {
        const merged = { ...last, text: last.text + delta }
        return [...items.slice(0, -1), merged]
      }
      return [...items, { key: nextKey(), kind: 'assistant', text: delta }]
    }
    case 'thinking_delta': {
      const delta = String((event as { text?: unknown }).text ?? '')
      if (last && last.kind === 'thinking') {
        const merged = { ...last, text: last.text + delta }
        return [...items.slice(0, -1), merged]
      }
      return [...items, { key: nextKey(), kind: 'thinking', text: delta }]
    }
    case 'tool_call':
      return [
        ...items,
        {
          key: nextKey(),
          kind: 'tool_call',
          id: String((event as { id?: unknown }).id ?? ''),
          name: String((event as { name?: unknown }).name ?? ''),
          args: (event as { args?: unknown }).args ?? {},
        },
      ]
    case 'tool_result':
      return [
        ...items,
        {
          key: nextKey(),
          kind: 'tool_result',
          id: String((event as { id?: unknown }).id ?? ''),
          name: String((event as { name?: unknown }).name ?? ''),
          content: (event as { content?: unknown }).content ?? null,
        },
      ]
    case 'summary':
      return [...items, { key: nextKey(), kind: 'summary', text: String((event as { text?: unknown }).text ?? '') }]
    case 'error':
      return [...items, { key: nextKey(), kind: 'error', text: String((event as { detail?: unknown }).detail ?? 'error') }]
    default:
      return items
  }
}

/** Convert a live `approval_required` event into a PendingApproval-shaped row so
 * the same inline card renders for both live and persisted approvals. Only the
 * fields the card actually reads are populated; the persisted-only attribution
 * fields (`turn_id`/`subject`/`approval_ref`) are simply absent for a live
 * approval — never read in the UI, so they stay optional on the type. */
export function approvalFromEvent(
  event: Extract<TurnEvent, { type: 'approval_required' }>,
  sessionId: string,
): PendingApproval {
  return {
    id: event.approval_id,
    session_id: sessionId,
    action: event.action,
    estimate: event.estimate,
    threshold: event.threshold,
    unit: event.unit,
    message: event.message,
    tool_name: event.tool_name,
    status: 'pending',
    created_at: new Date().toISOString(),
  }
}

// --- usage aggregation ------------------------------------------------------

const USAGE_KEYS = [
  'requests',
  'tool_calls',
  'input_tokens',
  'output_tokens',
  'total_tokens',
  'cache_read_tokens',
] as const

/** Sum two usage stats (token keys + cost), dropping zero-valued keys. Shares
 * the single `USAGE_KEYS` list with `cumulativeUsage` so the token-key set has
 * one source of truth. Returns `a` unchanged when `b` is absent. */
export function mergeUsage(a: UsageStat, b: UsageStat | null | undefined): UsageStat {
  if (!b) return a
  const out: Record<string, number> = {}
  for (const key of USAGE_KEYS) {
    const sum = Number(a[key] ?? 0) + Number(b[key] ?? 0)
    if (sum) out[key] = sum
  }
  const result = out as UsageStat
  const costA = typeof a.cost === 'number' ? a.cost : 0
  const costB = typeof b.cost === 'number' ? b.cost : 0
  if (typeof a.cost === 'number' || typeof b.cost === 'number') result.cost = costA + costB
  return result
}

/** Sum per-turn usage across a session's persisted messages (cumulative). */
export function cumulativeUsage(messages: MessageOut[]): UsageStat {
  const total: Record<string, number> = {}
  let cost = 0
  let anyCost = false
  for (const message of messages) {
    const usage = message.usage
    if (!usage) continue
    for (const key of USAGE_KEYS) {
      const value = Number(usage[key] ?? 0)
      if (value) total[key] = (total[key] ?? 0) + value
    }
    if (typeof usage.cost === 'number') {
      cost += usage.cost
      anyCost = true
    }
  }
  const result = total as UsageStat
  if (anyCost) result.cost = cost
  return result
}

/** The most-recent persisted per-turn usage (the last response), or null. */
export function lastTurnUsage(messages: MessageOut[]): UsageStat | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].usage) return messages[i].usage
  }
  return null
}
