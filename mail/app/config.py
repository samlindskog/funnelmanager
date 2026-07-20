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

    # Request auth is delegated to the auth service (service name "mail");
    # this backend validates incoming session tokens and issues none.
    auth_backend_url: str = "http://auth:8002"

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
