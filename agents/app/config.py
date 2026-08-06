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

    app_name: str = "Funnel Manager Agents"

    # Identity/authz plumbing (JWT parsing, audience=agents checks, token
    # exchange for the agents->mcp hop) is configured via the FM_* env vars read
    # by fm_runtime, not here.
    #
    # Phase 1 telemetry is likewise env-driven and read directly by
    # fm_runtime/logfire (NOT fields here — nothing in this Settings reads them):
    #   FM_LOGFIRE                   opt-in flag; "1" turns tracing on. Unset in
    #                                prod (logfire is then never imported).
    #                                `agents` is the ONE service that sets it.
    #   LOGFIRE_TOKEN                Logfire-cloud write token. Spans ship to the
    #                                cloud only when set (send_to_logfire=
    #                                'if-token-present'); unset in prod.
    #   OTEL_EXPORTER_OTLP_ENDPOINT  OTLP/gRPC collector (Tempo) for dual export
    #                                so app + mesh spans share one trace id, no
    #                                Logfire-cloud dependency. Optional.
    # fm_runtime.install() -> configure_tracing() wires these; app/main.py then
    # adds logfire.instrument_pydantic_ai() (fm_runtime can't depend on it).

    # This service owns a dedicated database (funnelmanager_agents). The database
    # is also created at startup if missing (see database.init_db), so pointing
    # this at any Postgres instance works too.
    database_url: str = (
        "postgresql+asyncpg://funnel:funnel@agents-db:5432/funnelmanager_agents"
    )

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost"

    # --- Runtime AI agent (the product feature) ------------------------------
    # The internal MCP server's streamable-HTTP mount. This is the runtime
    # agent's ONLY capability + situational-awareness surface — it acts
    # exclusively through these tools, never via direct backend calls.
    mcp_url: str = "http://mcp:8003/mcp"

    # The *runtime* agents' LLM (OpenAI). Key is read server-side only and never
    # sent to a client; the model that runs THIS build service is unrelated.
    openai_api_key: str = ""
    # OpenAI chat model the runtime agents plan/act with. Configurable; accepted
    # with or without a `openai:` provider prefix (see runner.build_model).
    # Default is a broadly-available current OpenAI chat model.
    agents_llm_model: str = "gpt-4o-mini"

    # Guardrails on a single runtime-agent run so a planning loop can never spin
    # unbounded (a resource guard in the spirit of Principle 4). A run that hits
    # a limit ends as `failed` with the reason recorded.
    run_request_limit: int = 25
    run_tool_calls_limit: int = 40
    # Hard wall-clock ceiling for one run (seconds); the run task is cancelled
    # and marked failed if exceeded.
    run_timeout_seconds: float = 600.0
    # How long a terminal run's final job event lingers for late-subscriber
    # replay before the in-memory registry prunes it.
    job_terminal_retention_seconds: float = 120.0

    # How often the in-process schedule poller wakes to fire due schedules
    # (seconds). Small enough that a one-shot ``at`` fires promptly, large enough
    # that idle polling is cheap. Multi-pod-safe: each firing is claimed atomically
    # at the DB level (schedules.claim_due_schedule) so a shared agents-db never
    # double-fires.
    schedule_poll_interval_seconds: float = 10.0

    # P4 Denial-of-Wallet floor (fix #2): the minimum interval a RECURRING schedule
    # may fire at. A recurring turn spins up an LLM + expensive MCP tool calls, and
    # a prompt-injection payload (the agent reasons over untrusted Apollo/Gmail
    # content) could otherwise plant a self-perpetuating ``* * * * *`` — ~1440
    # turns/day/session, reloaded from Postgres on every restart. The schedule tool
    # rejects any cron whose tightest implied interval is below this. NOTE: a full
    # per-run cost budget / circuit breaker (cumulative Apollo credits + OpenAI $,
    # OWASP LLM10) is the proper long-term control — TODO; a min-interval + a
    # per-session recurring cap is the Phase-3 mitigation.
    schedule_min_recurring_interval_minutes: int = 15
    # Bound on how many ACTIVE (scheduled) recurring schedules one session may hold
    # — a fire-rate cap complementing the min-interval (row-count alone is not a
    # rate bound). Also a total-pending cap below.
    schedule_max_recurring_per_session: int = 10
    # Bound on total pending schedules per session (one-shot + recurring) so a
    # looping agent can't flood the table with cheap one-shots.
    schedule_max_pending_per_session: int = 25

    # Single-writer control for the schedule poll loop (its captured-token cache +
    # serial-chat guard are per-pod, so exactly one pod must run it). Env
    # SCHEDULER_ENABLED:
    #   "auto" (default) — run on every pod EXCEPT an explicit canary
    #     (FM_DEPLOYMENT_VARIANT=canary), so the canary sharing the prod agents-db
    #     never competes with stable.
    #   "on"  — force the poller on (e.g. to drive a scheduling E2E on the canary
    #     while stable has no scheduler yet — safe only when no OTHER pod runs it).
    #   "off" — never run the poller here.
    scheduler_enabled: str = "auto"  # auto | on | off

    def should_run_scheduler(self, *, is_canary: bool) -> bool:
        mode = (self.scheduler_enabled or "auto").strip().lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return not is_canary  # auto

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def model_name(self) -> str:
        """Bare model name without a provider prefix (provider is always
        OpenAI here); ``openai:gpt-4o`` and ``gpt-4o`` both resolve to
        ``gpt-4o``."""
        raw = (self.agents_llm_model or "").strip()
        return raw.split(":", 1)[1] if ":" in raw else raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
