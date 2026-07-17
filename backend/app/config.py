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

    # Request auth is delegated to the auth service; this backend validates
    # incoming session tokens against it and issues no tokens of its own.
    auth_backend_url: str = "http://auth-backend:8002"

    # Internal URL for the leads service (Apollo lives there).
    leads_backend_url: str = "http://leads-backend:8001"
    database_url: str = "postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
