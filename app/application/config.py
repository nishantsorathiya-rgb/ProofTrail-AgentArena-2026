from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    mesh_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    mesh_model_id: str | None = None
    mesh_base_url: Literal["https://api.meshapi.ai/v1"] = "https://api.meshapi.ai/v1"
    tavily_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    database_url: str = Field(default="sqlite:///./data/prooftrail.db", repr=False)
    enable_live_requests: bool = False
    max_llm_calls_per_run: PositiveInt = 12
    max_searches_per_run: PositiveInt = 6
    max_fetches_per_run: PositiveInt = 10
    max_claims_per_run: int = Field(default=18, ge=1, le=100)
    max_retries_per_subquestion: int = Field(default=2, ge=0, le=2)
    max_research_tasks_per_run: int = Field(default=9, ge=1, le=30)
    max_parallel_fetches: PositiveInt = 3
    http_timeout_seconds: PositiveFloat = Field(default=12, allow_inf_nan=False)
    max_source_text_chars: PositiveInt = 12000
    fetch_max_bytes: PositiveInt = 1500000
    page_cache_ttl_seconds: PositiveInt = 3600
    tavily_max_attempts: int = Field(default=3, ge=1, le=5)
    tavily_backoff_initial_seconds: PositiveFloat = Field(default=0.5, allow_inf_nan=False)
    tavily_backoff_max_seconds: PositiveFloat = Field(default=10, allow_inf_nan=False)
    dns_timeout_seconds: PositiveFloat = Field(default=3, allow_inf_nan=False)
    max_research_subquestions: int = Field(default=3, ge=1, le=10)
    max_parallel_research_tasks: int = Field(default=3, ge=1, le=10)
    max_active_runs: int = Field(default=1, ge=1, le=4)
    max_pending_runs: int = Field(default=4, ge=0, le=20)
    workflow_timeout_seconds: PositiveFloat = Field(default=120, allow_inf_nan=False)
    max_parallel_verifications: int = Field(default=3, ge=1, le=10)
    max_verification_input_chars: int = Field(default=12000, ge=2000, le=48000)
    max_draft_input_chars: int = Field(default=24000, ge=2000, le=96000)

    @field_validator("database_url")
    @classmethod
    def local_sqlite_only(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"sqlite", "sqlite+aiosqlite"}
            or not url.path
            or not value.startswith(url.scheme + ":///")
            or url.netloc
            or url.query
            or url.fragment
        ):
            raise ValueError("A local SQLite database URL without query parameters is required.")
        return value
