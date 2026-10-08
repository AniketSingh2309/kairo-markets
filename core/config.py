"""Runtime configuration, loaded from environment variables / a local .env file.

Secrets (GROQ_API_KEY, ALPHA_VANTAGE_API_KEY) are only ever read from the
environment and are held as SecretStr so they never end up in logs or reprs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Settings field -> environment variable name.
_ENV_VARS: dict[str, str] = {
    "groq_api_key": "GROQ_API_KEY",
    "groq_model": "GROQ_MODEL",
    "llm_provider": "LLM_PROVIDER",
    "llm_temperature": "LLM_TEMPERATURE",
    "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
    "llm_max_attempts": "LLM_MAX_ATTEMPTS",
    "llm_backoff_base_seconds": "LLM_BACKOFF_BASE_SECONDS",
    "llm_max_tokens": "LLM_MAX_TOKENS",
    "llm_reasoning_effort": "LLM_REASONING_EFFORT",
    "data_provider": "DATA_PROVIDER",
    "data_agent_mode": "DATA_AGENT_MODE",
    "mock_data_path": "MOCK_DATA_PATH",
    "alpha_vantage_api_key": "ALPHA_VANTAGE_API_KEY",
    "tool_timeout_seconds": "TOOL_TIMEOUT_SECONDS",
    "tool_max_retries": "TOOL_MAX_RETRIES",
    "tool_backoff_base_seconds": "TOOL_BACKOFF_BASE_SECONDS",
    "max_tool_calls_per_request": "MAX_TOOL_CALLS_PER_REQUEST",
    "stale_after_hours": "STALE_AFTER_HOURS",
    "max_skew_hours": "MAX_SKEW_HOURS",
    "price_disagreement_pct": "PRICE_DISAGREEMENT_PCT",
    "db_path": "KAIRO_DB_PATH",
    "upstox_api_key": "UPSTOX_API_KEY",
    "upstox_api_secret": "UPSTOX_API_SECRET",
    "upstox_redirect_uri": "UPSTOX_REDIRECT_URI",
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    # --- LLM -------------------------------------------------------------
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    # "groq" = real Agno agents on Groq; "stub" = deterministic rule-based agents
    # (no network) used by unit tests and the offline eval mode.
    llm_provider: Literal["groq", "stub"] = "groq"
    llm_temperature: float = Field(0.1, ge=0.0, le=1.0)
    llm_timeout_seconds: float = Field(30.0, gt=0)
    llm_max_attempts: int = Field(2, ge=1, le=3)
    llm_backoff_base_seconds: float = Field(2.0, ge=0)
    # Completion budget per call. Groq's tokens-per-minute limit counts this reservation,
    # so keeping it tight matters on the free tier.
    llm_max_tokens: int = Field(1500, ge=256)
    # Only for reasoning models (e.g. openai/gpt-oss-*); leave unset for Llama.
    llm_reasoning_effort: Literal["low", "medium", "high"] | None = None

    # --- Data ------------------------------------------------------------
    data_provider: Literal["mock", "yahoo", "alpha_vantage"] = "mock"
    # "deterministic" = code decides which tools to call (default);
    # "llm" = an Agno tool-calling agent plans the calls, bounded by the same budget.
    data_agent_mode: Literal["deterministic", "llm"] = "deterministic"
    mock_data_path: Path | None = None
    alpha_vantage_api_key: SecretStr | None = None

    # --- Tool-call reliability -------------------------------------------
    tool_timeout_seconds: float = Field(2.0, gt=0)
    tool_max_retries: int = Field(2, ge=0, le=3)
    tool_backoff_base_seconds: float = Field(0.25, ge=0)
    max_tool_calls_per_request: int = Field(8, ge=1)

    # --- Freshness / consistency policy ----------------------------------
    stale_after_hours: float = Field(96.0, gt=0)  # 4 days tolerates weekends
    max_skew_hours: float = Field(24.0, gt=0)
    price_disagreement_pct: float = Field(0.5, gt=0)

    # --- Local storage (portfolio, alerts) ---------------------------------
    db_path: Path = PROJECT_ROOT / "data" / "kairo.db"

    # --- Real trading (optional): your own free Upstox developer app -------
    upstox_api_key: SecretStr | None = None
    upstox_api_secret: SecretStr | None = None
    upstox_redirect_uri: str = "http://127.0.0.1:8000/broker/upstox/callback"

    @classmethod
    def from_env(cls, env_file: str | Path | None = PROJECT_ROOT / ".env") -> Settings:
        if env_file is not None and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        values = {
            field: os.environ[var]
            for field, var in _ENV_VARS.items()
            if os.environ.get(var, "").strip() != ""
        }
        return cls(**values)

    def with_overrides(self, **overrides: object) -> Settings:
        return self.model_validate({**self.model_dump(), **overrides})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
