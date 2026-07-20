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

    # Internal service URL for the leads backend.
    leads_backend_url: str = "http://leads:8001"

    # Optional dev fallback for tool calls that arrive WITHOUT a token: act
    # as this server's own service identity (client-credentials token). When
    # disabled (default), tokenless calls fail — production clients must
    # supply the acting principal's token with every call.
    mcp_shared_login_fallback: bool = False

    # Host headers accepted by the MCP transport's DNS-rebinding protection.
    # Must cover every name clients dial: the compose service name (other
    # containers) and loopback (host-local clients).
    mcp_allowed_hosts: str = "mcp:8003,localhost:8003,127.0.0.1:8003"

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.mcp_allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
