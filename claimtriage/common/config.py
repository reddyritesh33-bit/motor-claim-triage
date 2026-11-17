"""Runtime configuration.

Every environment-specific value (credentials, endpoints, model identifiers,
prices) is injected through environment variables at runtime (requirement D4).
Nothing here has a provider endpoint or model id as a default; see .env.example.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: str = "claimtriage"
    log_level: str = "INFO"

    # --- internal endpoints (service discovery inside compose) ---
    redis_url: str = ""
    retriever_url: str = ""
    llm_gateway_url: str = ""

    # --- tracing ---
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = ""

    # --- LLM provider (used by llm-gateway only) ---
    llm_provider: str = "stub"  # stub | openai_compatible
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 3
    llm_backoff_base_s: float = 0.5
    llm_price_input_per_1m_usd: float = 0.0
    llm_price_output_per_1m_usd: float = 0.0
    stub_delay_ms_min: int = 800
    stub_delay_ms_max: int = 1500
    llm_extra_body: str = ""  # JSON merged into each provider request, e.g. {"reasoning_effort":"low"}
    llm_rate_limit_waits: int = 6  # 429s honoured per call before giving up (provider pacing, not failure)
    llm_rate_limit_max_wait_s: float = 65.0
    breaker_failure_threshold: int = 5
    breaker_reset_s: float = 30.0

    # --- embeddings / retrieval ---
    embedding_model: str = ""
    corpus_dir: str = "corpus"
    index_dir: str = "data/index"
    retrieval_cache_ttl_s: int = 3600
    retrieval_mode: str = "hybrid"  # hybrid | dense

    # --- agent loop bounds (A1) ---
    agent_max_steps: int = 12
    agent_max_tool_calls: int = 10
    agent_wall_clock_s: float = 120.0
    verify_max_retries: int = 2
    agent_state_char_budget: int = 14000  # policy text sent per coverage step (keeps prompts under provider limits)
    tool_timeout_s: float = 10.0
    llm_call_timeout_s: float = 75.0

    # --- prompt versions (L2) ---
    prompt_triage_version: str = "v1"
    prompt_coverage_version: str = "v2"
    prompt_verifier_version: str = "v1"

    # --- queue / worker ---
    agent_role: str = "coverage"  # triage | coverage
    job_lease_s: float = 180.0
    job_max_attempts: int = 2
    metrics_port: int = 9100

    # --- API policy ---
    rate_limit_per_min: int = 30
    run_cache_enabled: bool = True
    run_cache_ttl_s: int = 3600
    allow_eval_overrides: bool = False
    fault_injection_enabled: bool = False
    live_eval_sample_rate: float = 0.2

    # --- business thresholds ---
    fast_track_max_estimate_inr: int = 50000
    corpus_product_uin: str = "IRDAN134RP0004V03200708"


@lru_cache
def get_settings() -> Settings:
    return Settings()
