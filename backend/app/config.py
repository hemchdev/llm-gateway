import os
from functools import lru_cache
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


EnvironmentName = Literal["dev", "local", "test", "staging", "prod", "production"]


def _enable_docs_default() -> bool:
    value = os.getenv("ENABLE_DOCS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _encryption_key_default() -> str | None:
    value = os.getenv("ENCRYPTION_KEY")
    if value is None:
        dotenv_value = dotenv_values(".env").get("ENCRYPTION_KEY")
        value = str(dotenv_value) if dotenv_value is not None else None
    if value is None:
        return None
    return value.strip() or None


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    app_name: str = "LLM Gateway"
    environment: EnvironmentName = "dev"
    log_level: str = "INFO"
    redis_url: str = "redis://redis:6379/0"
    allowed_origins: str = "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000"
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)
    require_provider_api_keys: bool | None = None
    admin_api_key: SecretStr | None = None
    metrics_api_key: SecretStr | None = None
    enable_docs: bool = Field(default_factory=_enable_docs_default)
    encryption_key: str | None = Field(default_factory=_encryption_key_default)
    runtime_provider_key_prefix: str = "llm_gateway:runtime_providers"
    uvicorn_workers: int = Field(default=2, ge=1)
    default_model: str = "gpt-4o-mini"
    provider_preference: str = "openai,anthropic,gemini"
    classification_provider_preference: str = "openai,gemini,anthropic"
    long_form_generation_provider_preference: str = "anthropic,openai,gemini"
    latency_sensitive_request_classes: str = "classification"
    interactive_request_classes: str = "classification"
    deferrable_request_classes: str = "long_form_generation"
    queue_name: str = "llm_gateway:queue:ready"
    queue_delayed_name: str = "llm_gateway:queue:delayed"
    queue_idempotency_prefix: str = "llm_gateway:queue:idempotency"
    queue_base_backoff_seconds: float = Field(default=0.5, gt=0.0)
    queue_max_backoff_seconds: float = Field(default=30.0, gt=0.0)
    queue_jitter_seconds: float = Field(default=0.25, ge=0.0)
    queue_max_attempts: int = Field(default=5, ge=1)
    queue_worker_enabled: bool = False
    estimated_cost_per_1k_tokens: float = Field(default=0.001, ge=0.0)
    semantic_cache_enabled: bool = True
    semantic_cache_key_prefix: str = "llm_gateway:semantic_cache"
    semantic_cache_ttl_seconds: int = Field(default=3600, ge=1)
    cheap_model: str = "gpt-4o-mini"
    easy_request_classes: str = "classification"
    easy_prompt_max_words: int = Field(default=80, ge=1)
    tenant_limit_key_prefix: str = "llm_gateway:tenant_limits"
    tenant_rate_limit_per_window: int = Field(default=120, ge=1)
    tenant_rate_limit_window_seconds: int = Field(default=60, ge=1)
    tenant_monthly_budget_usd: float = Field(default=100.0, ge=0.0)
    tenant_budget_key_ttl_seconds: int = Field(default=2678400, ge=1)
    default_completion_token_estimate: int = Field(default=256, ge=1)
    chaos_key_prefix: str = "llm_gateway:chaos"
    openai_api_key: str | None = None
    openai_api_base: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "anthropic/claude-3-5-haiku-latest"
    gemini_model: str = "gemini/gemini-1.5-flash"
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    hedge_delay_seconds: float = Field(default=0.75, ge=0)
    health_window_seconds: int = Field(default=300, ge=1)
    health_redis_key_prefix: str = "llm_gateway:health"
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_reset_seconds: float = Field(default=30.0, gt=0)
    circuit_min_samples: int = Field(default=5, ge=1)
    circuit_error_rate_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    circuit_p95_latency_threshold_seconds: float = Field(default=10.0, gt=0.0)
    circuit_half_open_probe_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    chaos_enabled: bool = False
    chaos_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLM_GATEWAY_")

    @property
    def is_production_like(self) -> bool:
        return self.environment in {"staging", "prod", "production"}

    @property
    def provider_keys_required(self) -> bool:
        if self.require_provider_api_keys is not None:
            return self.require_provider_api_keys
        return self.is_production_like

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def providers(self) -> list[str]:
        return [
            provider.strip().lower()
            for provider in self.provider_preference.split(",")
            if provider.strip()
        ]

    @property
    def configured_provider_names(self) -> list[str]:
        ordered: list[str] = []
        for preference in (
            self.provider_preference,
            self.classification_provider_preference,
            self.long_form_generation_provider_preference,
        ):
            for provider in preference.split(","):
                provider_name = provider.strip().lower()
                if provider_name and provider_name not in ordered:
                    ordered.append(provider_name)
        return ordered

    def provider_api_key(self, provider: str) -> str | None:
        provider_name = provider.strip().lower()
        configured = {
            "openai": self.openai_api_key or os.getenv("OPENAI_API_KEY"),
            "anthropic": self.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"),
            "gemini": self.gemini_api_key or os.getenv("GEMINI_API_KEY"),
        }.get(provider_name)
        if configured is None:
            return None
        return configured.strip() or None

    def provider_api_base(self, provider: str) -> str | None:
        provider_name = provider.strip().lower()
        configured = {
            "openai": self.openai_api_base or os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL"),
        }.get(provider_name)
        if configured is None:
            return None
        return configured.strip() or None

    def admin_token(self) -> str | None:
        if self.admin_api_key is None:
            return None
        return self.admin_api_key.get_secret_value().strip() or None

    def metrics_token(self) -> str | None:
        if self.metrics_api_key is None:
            return self.admin_token()
        return self.metrics_api_key.get_secret_value().strip() or None

    def validate_startup(self) -> None:
        problems: list[str] = []
        if self.is_production_like and "*" in self.allowed_origins_list:
            problems.append("LLM_GATEWAY_ALLOWED_ORIGINS cannot include '*' in production-like environments")
        if self.is_production_like and self.admin_token() in {None, "", "dev-admin-token"}:
            problems.append("LLM_GATEWAY_ADMIN_API_KEY is required in production-like environments")
        if self.is_production_like and self.metrics_token() in {None, "", "dev-metrics-token"}:
            problems.append("LLM_GATEWAY_METRICS_API_KEY is required in production-like environments")
        if self.provider_keys_required:
            missing = [provider for provider in self.configured_provider_names if not self.provider_api_key(provider)]
            if missing:
                problems.append(f"missing API keys for configured providers: {', '.join(missing)}")
        if problems:
            raise RuntimeError("; ".join(problems))

    def provider_preferences_for(self, request_class: str) -> list[str]:
        preference = {
            "classification": self.classification_provider_preference,
            "long_form_generation": self.long_form_generation_provider_preference,
        }.get(request_class, self.provider_preference)
        return [provider.strip().lower() for provider in preference.split(",") if provider.strip()]

    @property
    def latency_sensitive_classes(self) -> set[str]:
        return {
            request_class.strip()
            for request_class in self.latency_sensitive_request_classes.split(",")
            if request_class.strip()
        }

    @property
    def interactive_classes(self) -> set[str]:
        return {
            request_class.strip()
            for request_class in self.interactive_request_classes.split(",")
            if request_class.strip()
        }

    @property
    def deferrable_classes(self) -> set[str]:
        return {
            request_class.strip()
            for request_class in self.deferrable_request_classes.split(",")
            if request_class.strip()
        }

    def request_delivery_mode(self, request_class: str) -> str:
        if request_class in self.deferrable_classes:
            return "deferrable"
        return "interactive"

    @property
    def easy_classes(self) -> set[str]:
        return {
            request_class.strip()
            for request_class in self.easy_request_classes.split(",")
            if request_class.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
