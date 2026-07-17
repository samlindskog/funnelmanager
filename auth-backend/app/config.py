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

    app_name: str = "Funnel Manager Auth"

    # Single shared login (see AUTH_USERNAME / AUTH_PASSWORD). AUTH_PASSWORD may be
    # a plain value or a bcrypt hash ($2...).
    auth_username: str = "admin"
    auth_password: str = "admin"

    # Opaque session tokens live in Redis with a native TTL (default 1 day).
    session_ttl_seconds: int = 60 * 60 * 24
    redis_url: str = "redis://redis:6379/0"

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
