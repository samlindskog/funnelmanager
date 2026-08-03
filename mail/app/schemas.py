from datetime import datetime

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    username: str
    role: str = ""
    # fm_origin of the acting principal ("user" | "agent") and the exchanging
    # client (azp) — recorded for attribution and used by the Principle-4
    # confirmation gate (an agent can never self-confirm an expensive action).
    origin: str = "user"
    actor: str = ""


class AccountOut(BaseModel):
    id: int
    email: str
    domain: str
    display_name: str = ""
    status: str
    last_error: str = ""
    backfill_done: bool = False
    backfill_authorized: bool = False
    backup_estimate_bytes: int = 0
    messages_total: int = 0
    last_sync_at: datetime | None = None
    connected_by: str = ""
    created_at: datetime | None = None
    message_count: int = 0
    inbox_count: int = 0
    sent_count: int = 0


class AttachmentOut(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str = ""
    size: int = 0


class MessageSummary(BaseModel):
    id: int
    account_id: int
    gmail_id: str
    thread_id: str = ""
    subject: str = ""
    snippet: str = ""
    from_addr: str = ""
    to_addrs: list[str] = []
    date: datetime | None = None
    label_ids: list[str] = []
    has_attachments: bool = False
    unread: bool = False
    is_deleted: bool = False


class MessageDetail(MessageSummary):
    cc_addrs: list[str] = []
    bcc_addrs: list[str] = []
    rfc822_message_id: str = ""
    body_text: str = ""
    body_html: str = ""
    size_estimate: int = 0
    attachments: list[AttachmentOut] = []


class MessagePage(BaseModel):
    items: list[MessageSummary]
    total: int
    page: int
    per_page: int


class OauthUrlOut(BaseModel):
    url: str


class SendRequest(BaseModel):
    to: list[str] = Field(min_length=1)
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = Field(default="", max_length=998)
    body_text: str = ""
    body_html: str = ""
    # Reply support: id of a stored message on the same account — the send
    # joins its thread and sets In-Reply-To/References.
    reply_to_message_id: int | None = None


class SyncTriggerOut(BaseModel):
    status: str = "started"


# --- Aggregated inbox / threads --------------------------------------------


class ThreadOut(BaseModel):
    thread_id: str
    account_id: int
    messages: list[MessageDetail] = []


# --- Backup gate -----------------------------------------------------------


class BackupEstimateOut(BaseModel):
    account_id: int
    messages_total: int
    estimated_bytes: int
    threshold_bytes: int
    over_threshold: bool
    backfill_authorized: bool
    backfill_done: bool


class BackupStartOut(BaseModel):
    account_id: int
    status: str  # "authorized" | "already_authorized"
    estimated_bytes: int
    messages_total: int


# --- Campaigns -------------------------------------------------------------


class RecipientIn(BaseModel):
    email: str
    apollo_id: str = ""
    name: str = ""


class CampaignSourceIn(BaseModel):
    search_id: str = ""
    label: str = ""
    recipients: list[RecipientIn] = []


class CampaignThrottle(BaseModel):
    per_domain_daily: int = Field(default=20, ge=1, le=10000)


class CampaignCreate(BaseModel):
    name: str = Field(default="", max_length=255)
    subject: str = Field(default="", max_length=998)
    body_text: str = ""
    body_html: str = ""
    send_strategy: str = Field(default="balanced")
    # Legacy per-campaign throttle. The per-domain daily cap is now a single
    # campaign-manager-wide GLOBAL setting (see the /campaigns/settings API), so
    # this is optional and IGNORED for the cap; kept only for backward-compatible
    # request payloads.
    throttle: CampaignThrottle | None = Field(default=None)
    sources: list[CampaignSourceIn] = []


class CampaignSettingsOut(BaseModel):
    """The campaign-manager-wide GLOBAL anti-spam per-domain daily send cap."""

    per_domain_daily: int


class CampaignSettingsUpdate(BaseModel):
    per_domain_daily: int = Field(ge=1, le=10000)


class CampaignSourceOut(BaseModel):
    id: int
    search_id: str = ""
    label: str = ""
    added_by: str = ""
    recipient_count: int = 0
    added_count: int = 0
    created_at: datetime | None = None


class CampaignStats(BaseModel):
    recipients_total: int = 0
    pending: int = 0
    sent: int = 0
    suppressed: int = 0
    failed: int = 0
    messages_sent: int = 0
    sent_today_by_domain: dict[str, int] = {}


class CampaignOut(BaseModel):
    id: int
    owner: str
    origin: str = "user"
    actor: str = ""
    name: str = ""
    status: str
    send_strategy: str = "balanced"
    throttle: dict = {}
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    last_error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    sources: list[CampaignSourceOut] = []
    stats: CampaignStats = Field(default_factory=CampaignStats)


class SourceMergeOut(BaseModel):
    """Result of adding/continuing a source: how the recipients landed after
    dedupe + suppression."""

    source_id: int
    submitted: int = 0
    added: int = 0
    duplicate_in_campaign: int = 0
    suppressed: int = 0


class ContactOut(BaseModel):
    email: str
    last_contacted: datetime | None = None
    campaign_ids: list[int] = []


class ContactedOut(BaseModel):
    emails: list[str] = []
    contacts: list[ContactOut] = []
