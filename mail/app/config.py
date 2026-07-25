from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"

# Least-privilege pair: read every message, send as the mailbox. Nothing here
# can modify or delete mail on the Google side.
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/gmail.send"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager Mail"

    # Identity/authz plumbing (JWT parsing, audience checks) is configured
    # via the FM_* env vars read by fm_runtime, not here.

    # This service owns a dedicated Postgres container (mail-db). The database
    # is also created at startup if missing (see database.init_db), so pointing
    # this at any Postgres instance works too.
    database_url: str = "postgresql+asyncpg://funnel:funnel@mail-db:5432/funnelmanager_mail"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    # Google OAuth client ("Web application" type). Connecting mailboxes is
    # disabled until both are set.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Redirect URI registered on the OAuth client. Blank = derived from
    # PUBLIC_BASE_URL, falling back to the dev nginx origin.
    oauth_redirect_url: str = ""
    public_base_url: str = ""

    # Where the browser lands after the OAuth callback (the hub Mail app).
    mail_app_url: str = "/mail"

    # Background sync cadence and how much backfill one cycle may do per
    # mailbox (pages of 100 messages).
    sync_interval_seconds: int = 180
    backfill_pages_per_cycle: int = 10

    # --- Full-backup-on-init gate (Principle 4) ----------------------------
    # Default size threshold above which a full mailbox backup must be
    # confirmed (bytes). Read through fm_runtime's confirmation_threshold so
    # FM_CONFIRM_MAIL_BACKUP_BYTES / MAIL_BACKUP_CONFIRM_BYTES can override at
    # runtime; this is only the fallback default (2 GiB).
    backup_confirm_bytes_default: int = 2 * 1024**3
    # Sampled-average-size estimate: how many messages to sample for avg bytes.
    backup_estimate_sample: int = 25

    # --- Campaigns ---------------------------------------------------------
    # Default per-domain daily send cap (throttle) when a campaign does not
    # specify one.
    campaign_per_domain_daily_default: int = 20
    # Recipient count above which STARTING a campaign is an expensive action
    # requiring confirmation (Principle 4; origin-aware). Fallback default.
    campaign_confirm_recipients_default: int = 1000
    # Campaign pacer cadence and per-(campaign,domain) sends per cycle, so a
    # campaign trickles rather than bursting its whole daily cap at once.
    campaign_interval_seconds: int = 60
    campaign_sends_per_domain_per_cycle: int = 10
    # Cross-campaign per-person suppression window (days). A person contacted
    # by ANY campaign within this many days is suppressed. 0 = suppress forever
    # once contacted.
    campaign_suppression_cooldown_days: int = 0

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def effective_redirect_url(self) -> str:
        if self.oauth_redirect_url:
            return self.oauth_redirect_url
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}/api/mail/oauth/callback"
        return "http://localhost:8000/api/mail/oauth/callback"

    @property
    def oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
