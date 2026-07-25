from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager Jobs"

    # Identity/authz plumbing (JWT parsing, audience=jobs checks, token
    # exchange) is configured via the FM_* env vars read by fm_runtime, not
    # here.

    # This service owns a dedicated database (funnelmanager_jobs). The database
    # is also created at startup if missing (see database.init_db), so pointing
    # this at any Postgres instance works too.
    database_url: str = "postgresql+asyncpg://funnel:funnel@jobs-db:5432/funnelmanager_jobs"

    # --- Producer registry (CONFIG-DRIVEN, principle: mail/others join later
    # without code changes) --------------------------------------------------
    # The apps whose ``/internal/jobs/v1/stream`` this service subscribes to,
    # and whose ``/internal/jobs/v1/{id}/{action}`` control API it proxies to.
    # Format: comma-separated ``name=base_url`` pairs; ``name`` is BOTH the
    # logical app name recorded on Job rows AND the token-exchange audience
    # (svc scope ``jobs-><name>`` must exist in Keycloak — v1: jobs->search,
    # jobs->agents). v1 producers are ``search`` + ``agents`` only; NEITHER
    # EXISTS YET, so the subscriber tolerates an unreachable producer and keeps
    # reconnecting (see subscriber.py).
    producers: str = Field(
        default="search=http://search:8000,agents=http://agents:8006",
        validation_alias="JOBS_PRODUCERS",
    )

    # Master switch for the background subscriber tasks (off = API-only, e.g.
    # for isolated MCP-API testing).
    subscriber_enabled: bool = True
    # Reconnect backoff for a down/absent producer stream (seconds).
    subscriber_backoff_initial_seconds: float = 1.0
    subscriber_backoff_max_seconds: float = 30.0
    # Per-request timeout for outbound control-proxy calls (seconds).
    control_timeout_seconds: float = 15.0

    @property
    def producer_map(self) -> dict[str, str]:
        """Parse ``producers`` into ``{app_name: base_url}``. Malformed pairs
        are skipped rather than crashing the service."""
        out: dict[str, str] = {}
        for pair in self.producers.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, base_url = pair.partition("=")
            name = name.strip()
            base_url = base_url.strip()
            if name and base_url:
                out[name] = base_url.rstrip("/")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
