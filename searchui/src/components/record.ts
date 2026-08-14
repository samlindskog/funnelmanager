import type { ApolloRecord } from '../types'

/** Shared, pure record-presentation helpers used by both the standard results
 * view and the grouped (top-people-per-company) results view. No React/state —
 * extracted from SearchResultsView so both renderers shape a record identically. */

/** Title · organization (people) / industry · domain (companies) — the row subtitle. */
export function secondaryText(record: ApolloRecord): string {
  if (record.entity_type === 'person') {
    const org = record.organization?.name
    return [record.title, org].filter(Boolean).join(' · ')
  }
  return [record.industry, record.domain].filter(Boolean).join(' · ')
}

/** Apollo emits this local-part when a person's email is locked / not revealed.
 * It is a placeholder, never a real address. The backend strips it from
 * normalized records, but the raw apollo_responses fallback below is NOT
 * placeholder-aware — filtering it here keeps the placeholder out of both the
 * contact chips and the CSV export (mirrors search-side `_is_placeholder_email`). */
function isPlaceholderEmail(value: string): boolean {
  return value.trim().toLowerCase().startsWith('email_not_unlocked@')
}

function emailFromValue(value: unknown): string | null {
  if (typeof value === 'string' && value.trim() && !isPlaceholderEmail(value)) {
    return value.trim()
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === 'string' && item.trim() && !isPlaceholderEmail(item)) {
        return item.trim()
      }
      if (item && typeof item === 'object') {
        const email = (item as Record<string, unknown>).email
        if (typeof email === 'string' && email.trim() && !isPlaceholderEmail(email)) {
          return email.trim()
        }
      }
    }
  }
  return null
}

export function resolvedRecordEmail(record: ApolloRecord): string | null {
  if (record.entity_type !== 'person') return null
  const direct = emailFromValue(record.email) || emailFromValue(record.emails)
  if (direct) return direct
  const entry = record.apollo_responses?.['/api/v1/people/match']
  const data = entry?.data
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const match = data as Record<string, unknown>
  const person = match.person
  if (person && typeof person === 'object' && !Array.isArray(person)) {
    const nested = person as Record<string, unknown>
    const fromPerson = emailFromValue(nested.email) || emailFromValue(nested.emails)
    if (fromPerson) return fromPerson
  }
  return emailFromValue(match.email) || emailFromValue(match.emails)
}

function phoneNumbersFromValue(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item.trim()
      if (item && typeof item === 'object') {
        const row = item as Record<string, unknown>
        return String(row.sanitized_number || row.raw_number || row.number || '').trim()
      }
      return ''
    })
    .filter(Boolean)
}

export function resolvedRecordPhone(record: ApolloRecord): string | null {
  if (record.entity_type === 'person') {
    const direct = phoneNumbersFromValue(record.phone_numbers)
    if (direct.length) return direct.join('; ')
    const entry = record.apollo_responses?.['/api/v1/people/match']
    const data = entry?.data
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const match = data as Record<string, unknown>
      const person = match.person
      if (person && typeof person === 'object' && !Array.isArray(person)) {
        const nested = phoneNumbersFromValue((person as Record<string, unknown>).phone_numbers)
        if (nested.length) return nested.join('; ')
      }
      const top = phoneNumbersFromValue(match.phone_numbers)
      if (top.length) return top.join('; ')
    }
    return null
  }
  return (record.phone || '').trim() || null
}

export function recordCompany(record: ApolloRecord): string | null {
  if (record.entity_type === 'person') return (record.organization?.name || '').trim() || null
  return (record.name || '').trim() || null
}

export function recordTitle(record: ApolloRecord): string | null {
  if (record.entity_type !== 'person') return null
  return (record.title || record.headline || '').trim() || null
}

/** Row-icon flags. For people the email/phone chips key off the authoritative,
 * placeholder-aware backend booleans (`has_email`/`has_phone` — a locked
 * `email_not_unlocked@…` never sets `has_email`), NOT the raw apollo_responses
 * fallbacks in resolvedRecord* (which are not placeholder-aware). LinkedIn keys
 * off the normalized `linkedin_url`. Companies have no email chip; their phone
 * comes from the normalized `phone` scalar. The narrower `apollo_enriched`
 * "enrichment revealed this" signal stays visible in the record detail pane. */
export function contactPresence(record: ApolloRecord): {
  linkedin: boolean
  email: boolean
  phone: boolean
} {
  const linkedin = Boolean((record.linkedin_url || '').trim())
  if (record.entity_type === 'person') {
    return {
      linkedin,
      email: Boolean(record.has_email),
      phone: Boolean(record.has_phone),
    }
  }
  return {
    linkedin,
    email: false,
    phone: Boolean((record.phone || '').trim()),
  }
}

export function csvEscape(value: string): string {
  // Neutralize spreadsheet formula injection (mirrors the backend's _csv_cell):
  // Excel/Sheets evaluate cells starting with = + - @ (or tab/CR) even when quoted.
  const cell = value && '=+-@\t\r'.includes(value[0]) ? `'${value}` : value
  if (/[",\r\n]/.test(cell)) return `"${cell.replace(/"/g, '""')}"`
  return cell
}
