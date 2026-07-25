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
    # contacted set for the `exclude_contacted` filter. Mail's endpoint lands in
    # Phase 5; until then the feature flag below stays off and the filter is a
    # graceful no-op. Any transport/auth failure also degrades to a no-op so a
    # search/export never fails because mail is unavailable.
    mail_backend_url: str = "http://mail:8004"
    # DEFERRED (Phase 5 dependency): enable only once mail exposes
    # GET /api/mail/contacts/contacted. Off by default so exclude_contacted is a
    # documented no-op rather than a hard dependency on an unbuilt endpoint.
    exclude_contacted_enabled: bool = False
    database_url: str = "postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
