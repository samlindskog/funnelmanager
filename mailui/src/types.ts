export interface User {
  username: string
  role: string
}

export interface MailAccount {
  id: number
  email: string
  domain: string
  display_name: string
  status: string
  last_error: string
  backfill_done: boolean
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
