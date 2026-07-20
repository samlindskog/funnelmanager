from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"

DEFAULT_WEB_APPS = (
    '[{"name": "Search", "description": "Apollo person/company search", '
    '"url": "/search"}, '
    '{"name": "Mail", "description": "Google Workspace inboxes & sending", '
    '"url": "/mail/"}]'
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager Auth"

    # Bootstrap admin credentials: on startup an `admin`-role user with this
    # username is created if missing. AUTH_PASSWORD may be a plain value or a
    # bcrypt hash ($2...). Changing AUTH_PASSWORD later does NOT update an
    # existing user — manage users through the admin API/UI after bootstrap.
    auth_username: str = "admin"
    auth_password: str = "admin"

    # Opaque session tokens live in Redis with a native TTL (default 1 day).
    session_ttl_seconds: int = 60 * 60 * 24
    redis_url: str = "redis://redis:6379/0"

    # OPA decision service. All authorization checks are evaluated there;
    # unreachable OPA fails closed (requests are denied).
    opa_url: str = "http://opa:8181"

    # JSON list of web apps shown on the post-login hub page:
    # [{"name": "...", "description": "...", "url": "..."}]
    # Blank (the compose default) falls back to DEFAULT_WEB_APPS.
    web_apps: str = ""

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
