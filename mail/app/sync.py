"""Per-account Gmail -> Postgres sync.

Two mechanisms cooperate per mailbox:

- **Backfill** walks the whole mailbox newest-first via ``messages.list`` with
  a page token persisted on the account row, a bounded number of pages per
  cycle, until exhausted (``backfill_done``).
- **Incremental** applies ``history.list`` from the stored ``history_id``:
  new messages are fetched in full, deletions flag ``is_deleted`` (rows are
  never removed — this store is the archive), label flips are applied. The
  anchor is captured at connect time, so mail arriving during a long backfill
  is still picked up. Gmail expires history after about a week; an expired
  anchor (HTTP 404) falls back to re-listing newest pages until only known
  ids appear, then re-anchors.

Progress commits page by page, so a crash mid-backfill loses at most one page.
A per-account asyncio lock keeps the background loop and manual sync triggers
from running the same mailbox concurrently.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.config import get_settings
from app.database import SessionLocal
from app.gmail import GmailAuthError, GmailClient, GmailError, parse_message
from app.models import MailAccount, MailMessage

logger = logging.getLogger(__name__)

# Hard cap for the expired-history catch-up walk (pages of 100).
_CATCH_UP_MAX_PAGES = 50


def apply_parsed(message: MailMessage, parsed: dict) -> None:
    """Copy a parse_message() dict onto a MailMessage row."""
    message.thread_id = parsed["thread_id"]
    message.history_id = parsed["history_id"]
    message.rfc822_message_id = parsed["rfc822_message_id"]
    message.internal_date = parsed["internal_date"]
    message.subject = parsed["subject"]
    message.snippet = parsed["snippet"]
    message.from_addr = parsed["from_addr"]
    message.to_addrs = json.dumps(parsed["to_addrs"])
    message.cc_addrs = json.dumps(parsed["cc_addrs"])
    message.bcc_addrs = json.dumps(parsed["bcc_addrs"])
    message.label_ids = json.dumps(parsed["label_ids"])
    message.body_text = parsed["body_text"]
    message.body_html = parsed["body_html"]
    message.attachments_json = json.dumps(parsed["attachments"])
    message.has_attachments = bool(parsed["attachments"])
    message.size_estimate = parsed["size_estimate"]
    message.is_deleted = False


class SyncManager:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None

    def _lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        settings = get_settings()
        while True:
            try:
                await self.sync_all()
            except Exception:
                logger.exception("Mail sync cycle failed")
            await asyncio.sleep(settings.sync_interval_seconds)

    async def sync_all(self) -> None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(MailAccount.id).where(MailAccount.status != "disabled")
            )
            account_ids = list(result.scalars())
        for account_id in account_ids:
            try:
                await self.sync_account(account_id)
            except Exception:
                logger.exception("Sync failed for mail account %s", account_id)

    async def sync_account(self, account_id: int) -> None:
        async with self._lock(account_id):
            await self._sync_locked(account_id)

    async def _sync_locked(self, account_id: int) -> None:
        settings = get_settings()
        async with SessionLocal() as session:
            account = await session.get(MailAccount, account_id)
            if account is None or account.status == "disabled":
                return
            async with GmailClient(settings, account) as client:
                try:
                    if not account.history_id:
                        # Safety net — normally anchored at connect time.
                        profile = await client.get_profile()
                        account.history_id = str(profile.get("historyId") or "")
                    else:
                        await self._sync_incremental(session, account, client)
                    if not account.backfill_done:
                        await self._sync_backfill(
                            session, account, client, settings.backfill_pages_per_cycle
                        )
                    account.status = "active"
                    account.last_error = ""
                except GmailAuthError as exc:
                    account.status = "error"
                    account.last_error = str(exc)
                    logger.warning("Mail account %s needs reconnect: %s", account.email, exc)
                except GmailError as exc:
                    account.status = "error"
                    account.last_error = exc.detail
                    logger.warning("Sync error for %s: %s", account.email, exc.detail)
                account.last_sync_at = datetime.now(timezone.utc)
                await session.commit()

    async def _sync_incremental(self, session, account: MailAccount, client: GmailClient) -> None:
        page_token = ""
        latest_history_id = account.history_id
        new_ids: list[str] = []
        deleted_ids: list[str] = []
        label_updates: dict[str, list[str]] = {}
        while True:
            try:
                payload = await client.list_history(account.history_id, page_token)
            except GmailError as exc:
                if exc.status_code == 404:
                    # Anchor expired — catch up by re-listing newest pages.
                    await self._catch_up_recent(session, account, client)
                    return
                raise
            for entry in payload.get("history") or []:
                for added in entry.get("messagesAdded") or []:
                    gmail_id = (added.get("message") or {}).get("id")
                    if gmail_id:
                        new_ids.append(gmail_id)
                for removed in entry.get("messagesDeleted") or []:
                    gmail_id = (removed.get("message") or {}).get("id")
                    if gmail_id:
                        deleted_ids.append(gmail_id)
                for change in (entry.get("labelsAdded") or []) + (entry.get("labelsRemoved") or []):
                    message = change.get("message") or {}
                    if message.get("id"):
                        label_updates[message["id"]] = message.get("labelIds") or []
            latest_history_id = str(payload.get("historyId") or latest_history_id)
            page_token = payload.get("nextPageToken") or ""
            if not page_token:
                break
        fetched = set()
        for gmail_id in dict.fromkeys(new_ids):
            if gmail_id not in deleted_ids:
                await self._fetch_and_store(session, account, client, gmail_id)
                fetched.add(gmail_id)
        if deleted_ids:
            await session.execute(
                update(MailMessage)
                .where(
                    MailMessage.account_id == account.id,
                    MailMessage.gmail_id.in_(deleted_ids),
                )
                .values(is_deleted=True)
            )
        for gmail_id, labels in label_updates.items():
            if gmail_id in fetched:
                continue  # already stored with fresh labels
            await session.execute(
                update(MailMessage)
                .where(
                    MailMessage.account_id == account.id,
                    MailMessage.gmail_id == gmail_id,
                )
                .values(label_ids=json.dumps(labels))
            )
        account.history_id = latest_history_id
        await session.commit()

    async def _sync_backfill(
        self, session, account: MailAccount, client: GmailClient, max_pages: int
    ) -> None:
        page_token = account.backfill_page_token
        for _ in range(max_pages):
            payload = await client.list_messages(page_token=page_token)
            ids = [m["id"] for m in payload.get("messages") or [] if m.get("id")]
            known = await self._known_ids(session, account.id, ids)
            for gmail_id in ids:
                if gmail_id not in known:
                    await self._fetch_and_store(session, account, client, gmail_id)
            page_token = payload.get("nextPageToken") or ""
            account.backfill_page_token = page_token
            if not page_token:
                account.backfill_done = True
            await session.commit()
            if account.backfill_done:
                return

    async def _catch_up_recent(self, session, account: MailAccount, client: GmailClient) -> None:
        profile = await client.get_profile()
        page_token = ""
        for _ in range(_CATCH_UP_MAX_PAGES):
            payload = await client.list_messages(page_token=page_token)
            ids = [m["id"] for m in payload.get("messages") or [] if m.get("id")]
            known = await self._known_ids(session, account.id, ids)
            fresh = [gmail_id for gmail_id in ids if gmail_id not in known]
            for gmail_id in fresh:
                await self._fetch_and_store(session, account, client, gmail_id)
            await session.commit()
            page_token = payload.get("nextPageToken") or ""
            if not fresh or not page_token:
                break
        account.history_id = str(profile.get("historyId") or "")
        await session.commit()

    async def _known_ids(self, session, account_id: int, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        result = await session.execute(
            select(MailMessage.gmail_id).where(
                MailMessage.account_id == account_id,
                MailMessage.gmail_id.in_(ids),
            )
        )
        return set(result.scalars())

    async def _fetch_and_store(
        self, session, account: MailAccount, client: GmailClient, gmail_id: str
    ) -> None:
        result = await session.execute(
            select(MailMessage).where(
                MailMessage.account_id == account.id,
                MailMessage.gmail_id == gmail_id,
            )
        )
        message = result.scalar_one_or_none()
        try:
            payload = await client.get_message(gmail_id)
        except GmailError as exc:
            if exc.status_code == 404:
                return  # gone between list and fetch
            raise
        parsed = parse_message(payload)
        if message is None:
            message = MailMessage(account_id=account.id, gmail_id=gmail_id)
            session.add(message)
        apply_parsed(message, parsed)


sync_manager = SyncManager()
