from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = SERVER_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager MCP"

    # Internal service URLs on the compose network.
    search_backend_url: str = "http://backend:8000"
    leads_backend_url: str = "http://leads-backend:8001"
    auth_backend_url: str = "http://auth-backend:8002"

    # The search backend validates every request token against the auth
    # backend, so this server logs in with the same shared credentials the UI
    # uses (see clients.AuthSession).
    auth_username: str = "admin"
    auth_password: str = "admin"

    # Host headers accepted by the MCP transport's DNS-rebinding protection.
    # Must cover every name clients dial: the compose service name (other
    # containers, e.g. OpenClaw) and loopback (host-local clients).
    mcp_allowed_hosts: str = "mcp-server:8003,localhost:8003,127.0.0.1:8003"

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.mcp_allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
