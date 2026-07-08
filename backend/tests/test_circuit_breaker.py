import asyncio

import pytest

from app.circuit_breaker import CircuitState, ProviderCircuitBreaker
from app.config import Settings
from app.health import ProviderHealth
from app.models import ChatCompletionChoice, ChatCompletionMessage, ChatCompletionResponse, ChatMessage, ChatRequest
from app.providers import complete_chat


class FakeHealthTracker:
    def __init__(self) -> None:
        self.health: dict[str, ProviderHealth] = {}
        self.successes: list[str] = []
        self.errors: list[str] = []

    async def summary(self, provider: str, tenant: str, feature: str) -> ProviderHealth:
        return self.health.get(
            provider,
            ProviderHealth(
                provider=provider,
                tenant=tenant,
                feature=feature,
                window_seconds=300,
                total=0,
                success=0,
                success_rate=1.0,
                p50_latency_seconds=None,
                p95_latency_seconds=None,
                p99_latency_seconds=None,
                errors={
                    "rate_limit": 0,
                    "timeout": 0,
                    "server_error": 0,
                    "content_filter": 0,
                    "auth_failure": 0,
                },
            ),
        )

    async def record_success(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
    ) -> None:
        self.successes.append(provider)

    async def record_error(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
        error_type: str,
    ) -> None:
        self.errors.append(provider)


def _health(provider: str, success_rate: float, p95: float, total: int = 10) -> ProviderHealth:
    return ProviderHealth(
        provider=provider,
        tenant="tenant-a",
        feature="classification",
        window_seconds=300,
        total=total,
        success=int(total * success_rate),
        success_rate=success_rate,
        p50_latency_seconds=0.1,
        p95_latency_seconds=p95,
        p99_latency_seconds=p95,
        errors={
            "rate_limit": 0,
            "timeout": total - int(total * success_rate),
            "server_error": 0,
            "content_filter": 0,
            "auth_failure": 0,
        },
    )


class FakeLiteLLMResponse:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    def model_dump(self) -> dict:
        return {
            "id": f"chatcmpl-test-{self.provider}",
            "created": 1,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.provider},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def _provider_for_model(settings: Settings, model: str) -> str:
    if model == settings.anthropic_model:
        return "anthropic"
    if model == settings.gemini_model:
        return "gemini"
    return "openai"


def _response(provider: str, model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"chatcmpl-test-{provider}",
        created=1,
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(role="assistant", content=provider),
                finish_reason="stop",
            )
        ],
    )


def _settings() -> Settings:
    return Settings(
        redis_url="redis://localhost:6379/0",
        provider_preference="openai,anthropic",
        classification_provider_preference="openai,anthropic",
        latency_sensitive_request_classes="",
        openai_api_key="test-openai-key",
        anthropic_api_key="test-anthropic-key",
        circuit_min_samples=2,
        circuit_error_rate_threshold=0.5,
        circuit_p95_latency_threshold_seconds=1.0,
        circuit_reset_seconds=5.0,
        circuit_half_open_probe_rate=1.0,
    )


async def _complete_with(
    settings: Settings,
    tracker: FakeHealthTracker,
    breaker: ProviderCircuitBreaker,
) -> ChatCompletionResponse:
    return await complete_chat(
        request=ChatRequest(
            model="gpt-4o-mini",
            messages=[ChatMessage(role="user", content="classify this")],
        ),
        settings=settings,
        tenant="tenant-a",
        feature="classification",
        request_id="req-1",
        health_tracker=tracker,
        breaker=breaker,
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_provider_breaker_cycles_closed_open_half_open_closed_with_external_provider_mock(monkeypatch) -> None:
    settings = _settings()
    tracker = FakeHealthTracker()
    now = 0.0
    calls: list[str] = []
    states_seen_by_executor: list[CircuitState] = []

    async def fake_acompletion(**payload):
        provider = _provider_for_model(settings, payload["model"])
        calls.append(provider)
        states_seen_by_executor.append(breaker.state_for(provider))
        return FakeLiteLLMResponse(provider, payload["model"])

    monkeypatch.setattr("app.providers.litellm.acompletion", fake_acompletion)

    breaker = ProviderCircuitBreaker(
        health_tracker=tracker,
        error_rate_threshold=settings.circuit_error_rate_threshold,
        p95_latency_threshold_seconds=settings.circuit_p95_latency_threshold_seconds,
        min_samples=settings.circuit_min_samples,
        reset_seconds=settings.circuit_reset_seconds,
        half_open_probe_rate=settings.circuit_half_open_probe_rate,
        clock=lambda: now,
        random_fn=lambda: 0.0,
    )

    response = asyncio.run(_complete_with(settings, tracker, breaker))
    assert response.choices[0].message.content == "openai"
    assert calls == ["openai"]
    assert breaker.state_for("openai") == CircuitState.CLOSED

    tracker.health["openai"] = _health("openai", success_rate=0.25, p95=0.2)
    response = asyncio.run(_complete_with(settings, tracker, breaker))
    assert response.choices[0].message.content == "anthropic"
    assert calls == ["openai", "anthropic"]
    assert breaker.state_for("openai") == CircuitState.OPEN

    now = 6.0
    tracker.health["openai"] = _health("openai", success_rate=1.0, p95=0.2)
    response = asyncio.run(_complete_with(settings, tracker, breaker))
    assert response.choices[0].message.content == "openai"
    assert calls == ["openai", "anthropic", "openai"]
    assert states_seen_by_executor == [CircuitState.CLOSED, CircuitState.CLOSED, CircuitState.HALF_OPEN]
    assert breaker.state_for("openai") == CircuitState.CLOSED


def test_provider_breaker_trips_on_p95_latency() -> None:
    tracker = FakeHealthTracker()
    tracker.health["openai"] = _health("openai", success_rate=1.0, p95=2.0)
    breaker = ProviderCircuitBreaker(
        health_tracker=tracker,
        error_rate_threshold=0.5,
        p95_latency_threshold_seconds=1.0,
        min_samples=2,
        reset_seconds=5.0,
        half_open_probe_rate=1.0,
    )

    decision = asyncio.run(breaker.allow_provider("openai", "tenant-a", "classification"))

    assert decision.allowed is False
    assert decision.state == CircuitState.OPEN
    assert decision.reason == "p95_latency"


def test_provider_breaker_half_open_can_throttle_probe_traffic() -> None:
    tracker = FakeHealthTracker()
    tracker.health["openai"] = _health("openai", success_rate=0.0, p95=0.2)
    now = 0.0
    breaker = ProviderCircuitBreaker(
        health_tracker=tracker,
        error_rate_threshold=0.5,
        p95_latency_threshold_seconds=1.0,
        min_samples=2,
        reset_seconds=5.0,
        half_open_probe_rate=0.1,
        clock=lambda: now,
        random_fn=lambda: 0.9,
    )

    opened = asyncio.run(breaker.allow_provider("openai", "tenant-a", "classification"))
    assert opened.state == CircuitState.OPEN
    assert opened.allowed is False

    now = 6.0
    probe = asyncio.run(breaker.allow_provider("openai", "tenant-a", "classification"))

    assert probe.state == CircuitState.HALF_OPEN
    assert probe.allowed is False
    assert probe.reason == "half_open_throttled"


def test_provider_breaker_half_open_failure_reopens_provider() -> None:
    tracker = FakeHealthTracker()
    tracker.health["openai"] = _health("openai", success_rate=0.0, p95=0.2)
    now = 0.0
    breaker = ProviderCircuitBreaker(
        health_tracker=tracker,
        error_rate_threshold=0.5,
        p95_latency_threshold_seconds=1.0,
        min_samples=2,
        reset_seconds=5.0,
        half_open_probe_rate=1.0,
        clock=lambda: now,
        random_fn=lambda: 0.0,
    )

    asyncio.run(breaker.allow_provider("openai", "tenant-a", "classification"))
    now = 6.0
    probe = asyncio.run(breaker.allow_provider("openai", "tenant-a", "classification"))
    assert probe.state == CircuitState.HALF_OPEN
    assert probe.allowed is True

    breaker.record_failure("openai", "server_error")

    assert breaker.state_for("openai") == CircuitState.OPEN
