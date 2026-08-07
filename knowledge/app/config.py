from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Funnel Manager Knowledge"
    database_url: str = "postgresql+asyncpg://funnel:funnel@knowledge-db:5432/funnelmanager_knowledge"

    # --- graph store (Neo4j, dedicated `knowledge-graph` container) -----------
    # Missing/unreachable Neo4j degrades gracefully (leads-Milvus pattern):
    # the service stays healthy; graph tools report unavailable.
    neo4j_uri: str = "bolt://knowledge-graph:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # --- LLM / embeddings (decision D4: OpenAI mid-tier extractor) ------------
    openai_api_key: str = ""
    knowledge_llm_model: str = "gpt-4.1"
    knowledge_small_llm_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    openai_max_retries: int = 6

    # --- source services (read-only deps; exchange edges knowledge->{svc}) ----
    mail_backend_url: str = "http://mail:8004"
    search_backend_url: str = "http://search:8000"
    agents_backend_url: str = "http://agents:8006"

    # --- records sync ---------------------------------------------------------
    sync_interval_seconds: float = 300.0
    sync_page_size: int = 200
    # mail messages: how many newest pages are re-walked each cycle even past
    # the watermark (catches label flips / deletions near the head).
    mail_head_pages: int = 2
    # Embedding-backfill budget: rows embedded per cycle across all sources
    # (batched 256 at a time). Embeddings are cheap (text-embedding-3-small);
    # this bounds burst API pressure, not spend.
    embed_backfill_per_cycle: int = 2000
    # Per-cycle page budget per account; a capped walk resumes next cycle
    # (initial backfill) or holds the watermark (incremental) — never skips.
    mail_pages_per_cycle: int = 200

    # --- fuzzy matching -------------------------------------------------------
    # Cosine-similarity floor for fuzzy-column matches (text-embedding-3-small
    # typically puts related short strings at ~0.3-0.6). Tool-configured here;
    # individual queries may override per filter.
    fuzzy_threshold_default: float = 0.30
    query_limit_default: int = 50
    query_limit_max: int = 500

    # --- graph ingestion (LLM cost surface) -----------------------------------
    # Sources whose NEW records are also distilled into main-graph episodes.
    # All sources always sync into the records store; this only controls the
    # (expensive) Graphiti extraction. mail_messages is deliberately opt-in.
    graph_ingest_sources: str = "searches,mail_campaigns"
    # Ingest agent-session transcripts into the per-user session graphs.
    session_ingest_enabled: bool = True
    # Auto-loop cost guards: at most this many episodes per cycle, and if the
    # backlog exceeds the P4 threshold the auto loop STOPS and waits for an
    # explicitly confirmed knowledge_sync (estimate -> confirm).
    graph_auto_batch: int = 25
    sync_confirm_episodes: float = 200.0

    # Per-user session graph namespace prefix + the shared application graph.
    main_group_id: str = "main"
    user_group_prefix: str = "user-"

    @property
    def graph_source_set(self) -> set[str]:
        return {s.strip() for s in self.graph_ingest_sources.split(",") if s.strip()}

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
