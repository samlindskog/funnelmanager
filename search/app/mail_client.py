"""Search -> mail hop: read the contacted set for the ``exclude_contacted`` filter.

The authoritative contacted set is owned by ``mail`` and exposed at
``GET /api/mail/contacts/contacted`` (Phase 5). This client consults it so search
can pre-drop already-messaged leads from a result list / export.

Failure is a **graceful no-op**: whenever mail is unreachable or the token
exchange is refused, ``contacted_emails`` returns ``None`` and the caller treats
that as "filter unavailable, drop nothing" so a search/export never fails because
of a down dependency. ``None`` is deliberately distinct from an empty set. The
*authoritative* dedupe still happens at send time inside mail; this is only a
convenience pre-filter.

Auth: the hop uses ``InternalClient`` with audience ``mail`` (svc scope
``search->mail``), exchanging the acting principal's token so mail sees the same
human. No Apollo, no leads involvement.
"""

from __future__ import annotations

import logging

import httpx
from fm_runtime import ExchangeError, InternalClient

from app.config import Settings

logger = logging.getLogger(__name__)


class MailClient:
    AUDIENCE = "mail"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def contacted_emails(self, campaign_id: str | None = None) -> set[str] | None:
        """Lowercased set of already-contacted emails, or ``None`` when the
        filter is unavailable (feature off, mail down, or exchange refused).

        ``None`` is deliberately distinct from an empty set: empty means "mail
        answered, nobody contacted yet"; ``None`` means "could not consult mail".

        ``campaign_id`` scopes the exclusion to one campaign; omit it (``None``)
        to exclude anyone contacted by *any* campaign.
        """
        params = {"campaign_id": campaign_id} if campaign_id else None
        try:
            async with InternalClient(
                self.settings.mail_backend_url, self.AUDIENCE, timeout=15.0
            ) as client:
                response = await client.get(
                    "/api/mail/contacts/contacted", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, ExchangeError) as exc:
            # Never fail the search/export because mail is unavailable OR the
            # search->mail token exchange is refused (missing svc scope, KC down):
            # ExchangeError is raised by the broker, not httpx. Degrade to no-op.
            logger.warning("exclude_contacted: mail contacted-set lookup failed: %s", exc)
            return None
        return _emails_from_payload(payload)


def _emails_from_payload(payload: object) -> set[str]:
    """Extract lowercased emails from mail's ``ContactedOut``
    (``{"emails": [...], "contacts": [{email, ...}]}``). Stays tolerant of a bare
    list or a contacts-only shape so an additive mail change can't break search."""
    emails: set[str] = set()

    def _add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            emails.add(value.strip().lower())

    items: object = payload
    if isinstance(payload, dict):
        items = payload.get("emails") or payload.get("contacts") or []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                _add(item.get("email"))
    return emails


__all__ = ["MailClient"]
