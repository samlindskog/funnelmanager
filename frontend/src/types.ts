export interface User {
  username: string
  role: string
}

export interface AppLink {
  name: string
  description: string
  url: string
}

export interface Grant {
  service: string
  methods: string[]
  path_prefix: string
}

export interface Role {
  name: string
  description: string
  grants: Grant[]
  created_at?: string | null
}

export interface ChannelLink {
  channel: string
  device_id: string
  username: string
  display_name?: string
  linked_at?: string
}

export interface UserDetail extends User {
  created_at?: string | null
  channels: ChannelLink[]
}

export interface AccountRequest {
  username: string
  requested_at?: string
  last_seen_at?: string
}

export interface ChannelRequest {
  channel: string
  device_id: string
  display_name?: string
  requested_at?: string
  last_seen_at?: string
}

// ---------------------------------------------------------------------------
// Search app — Apollo records and search history (search backend)
// ---------------------------------------------------------------------------

export type EntityType = 'people' | 'companies'

export interface OrganizationSummary {
  id?: string
  name?: string | null
  domain?: string | null
  linkedin_url?: string | null
}

export interface ApolloEndpointResponse {
  received_at?: string | null
  data: Record<string, unknown>
}

export type ApolloEnrichedFlags = {
  linkedin: boolean
  email: boolean
  phone: boolean
}

export interface PersonRecord {
  id?: string
  /** MongoDB lead `_id` used for hydrate / refresh. */
  mongo_id?: string | null
  embedding?: boolean
  entity_type: 'person'
  name: string
  first_name?: string
  last_name?: string
  title?: string | null
  headline?: string | null
  linkedin_url?: string | null
  email?: string | null
  emails?: unknown
  phone_numbers?: unknown
  has_email?: boolean
  has_phone?: boolean
  city?: string | null
  state?: string | null
  country?: string | null
  organization?: OrganizationSummary
  apollo_enriched?: ApolloEnrichedFlags
  /** Preferred entity payload used for the detail UI (enriched when available). */
  raw?: Record<string, unknown>
  /** Raw Apollo payloads keyed by endpoint path (only endpoints that have been called). */
  apollo_responses?: Record<string, ApolloEndpointResponse>
}

export interface CompanyRecord {
  id?: string
  /** MongoDB lead `_id` used for hydrate / refresh. */
  mongo_id?: string | null
  embedding?: boolean
  account_id?: string
  organization_id?: string
  entity_type: 'company'
  name: string
  domain?: string | null
  website_url?: string | null
  linkedin_url?: string | null
  industry?: string | null
  keywords?: string[]
  employee_count?: number | null
  city?: string | null
  state?: string | null
  country?: string | null
  short_description?: string | null
  phone?: string | null
  apollo_enriched?: ApolloEnrichedFlags
  raw?: Record<string, unknown>
  apollo_responses?: Record<string, ApolloEndpointResponse>
}

export type ApolloRecord = PersonRecord | CompanyRecord

export interface SearchHistorySummary {
  id: number
  query: string
  entity_type: EntityType
  page: number
  per_page: number
  total_results: number
  created_at: string
}

export interface SearchHistoryDetail extends SearchHistorySummary {
  results: ApolloRecord[]
}

export interface SearchResponse {
  history: SearchHistoryDetail
  pagination: {
    page?: number
    per_page?: number
    total_entries?: number
    total_pages?: number
  }
}
