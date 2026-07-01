from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    app_name: str = "LLM Gateway"
    environment: str = "local"
    log_level: str = "INFO"
    redis_url: str = "redis://redis:6379/0"
    default_model: str = "gpt-4o-mini"
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    hedge_delay_seconds: float = Field(default=0.75, ge=0)
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_reset_seconds: float = Field(default=30.0, gt=0)
    chaos_enabled: bool = False
    chaos_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLM_GATEWAY_")


@lru_cache
def get_settings() -> Settings:
    return Settings()
