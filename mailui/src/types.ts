export interface User {
  username: string
}

export interface MailAccount {
  id: number
  email: string
  domain: string
  display_name: string
  status: string
  last_error: string
  backfill_done: boolean
  backfill_authorized: boolean
  backup_estimate_bytes: number
  messages_total: number
  last_sync_at: string | null
  connected_by: string
  created_at: string | null
  message_count: number
  inbox_count: number
  sent_count: number
}

export interface MailAttachment {
  attachment_id: string
  filename: string
  mime_type: string
  size: number
}

export interface MailMessageSummary {
  id: number
  account_id: number
  gmail_id: string
  thread_id: string
  subject: string
  snippet: string
  from_addr: string
  to_addrs: string[]
  date: string | null
  label_ids: string[]
  has_attachments: boolean
  unread: boolean
  is_deleted: boolean
}

export interface MailMessageDetail extends MailMessageSummary {
  cc_addrs: string[]
  bcc_addrs: string[]
  rfc822_message_id: string
  body_text: string
  body_html: string
  size_estimate: number
  attachments: MailAttachment[]
}

export interface MailMessagePage {
  items: MailMessageSummary[]
  total: number
  page: number
  per_page: number
}

export interface MailSendRequest {
  to: string[]
  cc?: string[]
  bcc?: string[]
  subject: string
  body_text: string
  body_html?: string
  reply_to_message_id?: number | null
}

export interface MailThread {
  thread_id: string
  account_id: number
  messages: MailMessageDetail[]
}

// --- Backup gate (Principle 4) ---------------------------------------------

export interface BackupEstimate {
  account_id: number
  messages_total: number
  estimated_bytes: number
  threshold_bytes: number
  over_threshold: boolean
  backfill_authorized: boolean
  backfill_done: boolean
}

export interface BackupStart {
  account_id: number
  status: string
  estimated_bytes: number
  messages_total: number
}

// --- Confirmation gate (Principle 4) ---------------------------------------

/** Structured `detail` of a 409 `confirmation_required` (the human confirm flow). */
export interface ConfirmationRequired {
  error: 'confirmation_required'
  estimate: number
  threshold: number
  unit: string
  action: string
  confirm_token: string
  message: string
  meta?: Record<string, unknown>
}

/** Structured `detail` of a 409 `human_approval_required` (agent-origin only). */
export interface HumanApprovalRequired {
  error: 'human_approval_required'
  needs_human_approval: true
  estimate: number
  threshold: number
  unit: string
  action: string
  subject: string
  approval_ref: string
  message: string
}

// --- Campaigns -------------------------------------------------------------

export interface CampaignRecipientIn {
  email: string
  apollo_id?: string
  name?: string
}

export interface CampaignSourceIn {
  search_id?: string
  label?: string
  recipients: CampaignRecipientIn[]
}

export interface CampaignSource {
  id: number
  search_id: string
  label: string
  added_by: string
  recipient_count: number
  added_count: number
  created_at: string | null
}

export interface CampaignStats {
  recipients_total: number
  pending: number
  sent: number
  suppressed: number
  failed: number
  messages_sent: number
  sent_today_by_domain: Record<string, number>
}

export interface Campaign {
  id: number
  owner: string
  origin: string
  actor: string
  name: string
  status: string
  send_strategy: string
  // The cap is now a single GLOBAL setting (see CampaignSettings); the backend
  // may still echo a per-campaign throttle here, but the UI no longer reads it.
  throttle?: { per_domain_daily?: number }
  subject: string
  body_text: string
  body_html: string
  last_error: string
  created_at: string | null
  updated_at: string | null
  sources: CampaignSource[]
  stats: CampaignStats
}

export interface CampaignCreate {
  name?: string
  subject?: string
  body_text?: string
  body_html?: string
  send_strategy: string
  sources: CampaignSourceIn[]
}

/**
 * The per-domain daily send cap is a single GLOBAL setting (get/set on the
 * campaign manager page), applied to all campaigns — not a per-campaign field.
 */
export interface CampaignSettings {
  per_domain_daily: number
}

export interface SourceMerge {
  source_id: number
  submitted: number
  added: number
  duplicate_in_campaign: number
  suppressed: number
}

// --- Saved searches (source lists, read from the search backend) -----------

export interface SavedSearch {
  id: number
  query: string
  entity_type: string
  page: number
  per_page: number
  total_results: number
  created_at: string
  username: string
  origin: string
  actor: string
}
