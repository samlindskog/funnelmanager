from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"

# Historic placeholder that shipped in example env files; never accept it as a
# real secret — the webhook endpoint is publicly reachable.
PLACEHOLDER_WEBHOOK_SECRET = "change-me-apollo-webhook-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Funnel Manager Leads"

    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/api/v1"
    # Public origin Apollo can POST async webhooks to (nginx → leads webhook only).
    # Example: https://your-host.example or an ngrok URL in local dev.
    public_base_url: str = ""
    apollo_webhook_secret: str = ""
    mongodb_url: str = "mongodb://mongo:27017"
    mongodb_db: str = "funnelmanager_leads"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"
    # Negative-resolve cache: how long a "genuine Apollo miss" marker for a domain
    # lives before it expires (Mongo TTL index). Bounds staleness for a company that
    # later appears in Apollo. Default 7 days.
    resolve_miss_ttl_seconds: int = 604800

    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    # Retry budget for the OpenAI embed client. Raised above the SDK default (2)
    # so sustained 429s under concurrent embedding back off and recover (the SDK
    # honors Retry-After) rather than exhausting and dropping embeddings.
    openai_max_retries: int = 6
    # Max concurrent OpenAI embed calls. The slow embed stage now runs lock-free
    # (only the short Milvus upsert serializes on the priority gate), so this can
    # be raised without starving interactive similarity queries. Env-tunable.
    embed_concurrency: int = 8
    # Max page-sized embedding batches running at once during a live search.
    embed_batch_concurrency: int = 4
    # Backpressure: how many Apollo pages of mongo-ids may buffer between the
    # ingest walk and the embedding consumer before the walk blocks on put (so
    # Apollo fetching pauses until embedding/Milvus catches up). Small on purpose.
    ingest_embed_queue_max_pages: int = 3
    # Amortized Milvus write-pressure retry budget PER embedding stream (seconds),
    # shared across every chunk of that stream. Bounds total backoff so a 50k
    # stream under sustained pressure can't retry for hours (a per-chunk budget
    # would multiply by the chunk count); once spent, pressure-failures fail fast
    # to the honest item_error path so the stream ends promptly.
    embed_write_pressure_budget_seconds: float = 300.0
    milvus_uri: str = "http://milvus:19530"
    milvus_collection: str = "leads_embeds_v2"

    @field_validator(
        "apollo_api_key",
        "public_base_url",
        "apollo_webhook_secret",
        "openai_api_key",
        "milvus_uri",
        "milvus_collection",
        mode="before",
    )
    @classmethod
    def strip_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def apollo_configured(self) -> bool:
        return bool(self.apollo_api_key)

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def apollo_webhook_secret_configured(self) -> bool:
        return bool(self.apollo_webhook_secret) and self.apollo_webhook_secret != PLACEHOLDER_WEBHOOK_SECRET

    def people_match_async_webhook_url(self) -> str:
        """Webhook Apollo calls for phone reveal and/or waterfall enrichment results.

        Points at leads directly via nginx (`/api/leads/webhooks/...`), not the
        search backend. Secret is in the path so Apollo's webhook_url query param
        stays unambiguous.
        """
        from urllib.parse import quote

        base = self.public_base_url.rstrip("/")
        if not base:
            raise ValueError(
                "PUBLIC_BASE_URL is not configured. Set it to a publicly reachable "
                "HTTPS origin (e.g. an ngrok URL) so Apollo can deliver "
                "waterfall/phone webhooks."
            )
        if not self.apollo_webhook_secret_configured:
            raise ValueError(
                "APOLLO_WEBHOOK_SECRET is not configured (or is the repo-published "
                "placeholder). Set a random secret so webhook deliveries can be "
                "authenticated."
            )
        secret = quote(self.apollo_webhook_secret, safe="")
        return f"{base}/api/leads/webhooks/apollo/{secret}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
