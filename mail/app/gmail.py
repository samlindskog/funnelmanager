"""Minimal Gmail REST + OAuth client (httpx, no Google SDKs).

Only the mail service talks to Google. OAuth tokens live on the MailAccount
row; ``GmailClient`` refreshes the access token in place on that ORM object —
the caller's session persists it on its next commit.
"""

import base64
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from urllib.parse import urlencode

import httpx

from app.config import GMAIL_SCOPES, Settings

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class GmailAuthError(GmailError):
    """Refresh token rejected (revoked/expired) — the mailbox needs a reconnect."""


def build_auth_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.effective_redirect_url,
        "response_type": "code",
        "scope": GMAIL_SCOPES,
        # offline + consent forces Google to (re)issue a refresh token.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(settings: Settings, code: str) -> dict:
    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.effective_redirect_url,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URL, data=data)
    if response.status_code >= 400:
        raise GmailError(f"OAuth code exchange failed: {response.text[:300]}")
    return response.json()


async def refresh_access_token(settings: Settings, refresh_token: str) -> dict:
    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URL, data=data)
    if response.status_code >= 400:
        body = response.text[:300]
        if "invalid_grant" in body:
            raise GmailAuthError(f"Google refresh token rejected: {body}")
        raise GmailError(f"Token refresh failed: {body}")
    return response.json()


class GmailClient:
    """Async Gmail API wrapper bound to one account's tokens."""

    def __init__(self, settings: Settings, account) -> None:
        self._settings = settings
        self._account = account
        self._http = httpx.AsyncClient(timeout=60.0)

    async def __aenter__(self) -> "GmailClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        account = self._account
        now = datetime.now(timezone.utc)
        if account.access_token and account.token_expiry and account.token_expiry - timedelta(seconds=60) > now:
            return account.access_token
        payload = await refresh_access_token(self._settings, account.refresh_token)
        account.access_token = str(payload["access_token"])
        account.token_expiry = now + timedelta(seconds=int(payload.get("expires_in", 3600)))
        return account.access_token

    async def _json(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        token = await self._access_token()
        response = await self._http.request(
            method,
            f"{API_BASE}{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            # Cached access token went stale mid-flight — force one refresh.
            self._account.access_token = ""
            token = await self._access_token()
            response = await self._http.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise GmailError(
                f"Gmail API {method} {path} -> {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json()

    async def get_profile(self) -> dict:
        return await self._json("GET", "/profile")

    async def list_messages(self, page_token: str = "", max_results: int = 100) -> dict:
        params: dict = {"maxResults": max_results, "includeSpamTrash": "false"}
        if page_token:
            params["pageToken"] = page_token
        return await self._json("GET", "/messages", params=params)

    async def get_message(self, gmail_id: str) -> dict:
        return await self._json("GET", f"/messages/{gmail_id}", params={"format": "full"})

    async def list_history(self, start_history_id: str, page_token: str = "") -> dict:
        params: dict = {"startHistoryId": start_history_id, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        return await self._json("GET", "/history", params=params)

    async def get_attachment(self, gmail_id: str, attachment_id: str) -> dict:
        return await self._json("GET", f"/messages/{gmail_id}/attachments/{attachment_id}")

    async def send(self, raw: str, thread_id: str = "") -> dict:
        body: dict = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return await self._json("POST", "/messages/send", json_body=body)


# --- payload parsing -------------------------------------------------------


def decode_base64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_text(data: str) -> str:
    return decode_base64url(data).decode("utf-8", errors="replace")


def _address_list(value: str) -> list[str]:
    out = []
    for name, addr in getaddresses([value or ""]):
        if addr:
            out.append(formataddr((name or None, addr)))
    return out


def _walk_part(part: dict, out: dict) -> None:
    mime = part.get("mimeType") or ""
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename and body.get("attachmentId"):
        out["attachments"].append(
            {
                "attachment_id": body["attachmentId"],
                "filename": filename,
                "mime_type": mime,
                "size": int(body.get("size") or 0),
            }
        )
    elif body.get("data"):
        if mime == "text/plain" and not out["text"]:
            out["text"] = _decode_text(body["data"])
        elif mime == "text/html" and not out["html"]:
            out["html"] = _decode_text(body["data"])
    for sub in part.get("parts") or []:
        _walk_part(sub, out)


def parse_message(payload: dict) -> dict:
    """Flatten a ``users.messages.get format=full`` payload for storage."""
    part = payload.get("payload") or {}
    headers: dict[str, str] = {}
    for header in part.get("headers") or []:
        headers.setdefault(str(header.get("name") or "").lower(), str(header.get("value") or ""))
    bodies: dict = {"text": "", "html": "", "attachments": []}
    _walk_part(part, bodies)
    internal_ms = int(payload.get("internalDate") or 0)
    return {
        "gmail_id": str(payload.get("id") or ""),
        "thread_id": str(payload.get("threadId") or ""),
        "history_id": str(payload.get("historyId") or ""),
        "label_ids": payload.get("labelIds") or [],
        "snippet": str(payload.get("snippet") or ""),
        "size_estimate": int(payload.get("sizeEstimate") or 0),
        "internal_date": (
            datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc) if internal_ms else None
        ),
        "subject": headers.get("subject", ""),
        "from_addr": headers.get("from", ""),
        "to_addrs": _address_list(headers.get("to", "")),
        "cc_addrs": _address_list(headers.get("cc", "")),
        "bcc_addrs": _address_list(headers.get("bcc", "")),
        "rfc822_message_id": headers.get("message-id", ""),
        "body_text": bodies["text"],
        "body_html": bodies["html"],
        "attachments": bodies["attachments"],
    }


def build_mime(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body_text: str,
    body_html: str = "",
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """Build an RFC-822 message and return it base64url-encoded for send."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
