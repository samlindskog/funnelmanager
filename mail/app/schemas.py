from datetime import datetime

from pydantic import BaseModel, Field


class UserOut(BaseModel):
    username: str
    role: str = ""


class AccountOut(BaseModel):
    id: int
    email: str
    domain: str
    display_name: str = ""
    status: str
    last_error: str = ""
    backfill_done: bool = False
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
