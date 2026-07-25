from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager"

    # Identity/authz plumbing (JWT parsing, token exchange) is configured via
    # the FM_* env vars read by fm_runtime, not here.

    # Internal URL for the leads service (Apollo lives there).
    leads_backend_url: str = "http://leads:8001"
    # Internal URL for the mail service — the search->mail hop reads mail's
    # contacted set (GET /api/mail/contacts/contacted, shipped in Phase 5) for the
    # `exclude_contacted` filter. The hop exchanges the acting principal's token
    # for the `mail` audience (svc scope search->mail). Any transport/auth failure
    # degrades to a no-op (drop nothing) so a search/export never fails because
    # mail is unavailable; the authoritative dedupe still happens at send time in
    # mail.
    mail_backend_url: str = "http://mail:8004"
    database_url: str = "postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
