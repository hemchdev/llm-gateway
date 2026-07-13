import asyncio
import json

from app.circuit_breaker import ProviderCircuitBreaker
from app.config import Settings
from app.health import ProviderHealth
from app.models import ChatCompletionChoice, ChatCompletionMessage, ChatCompletionResponse, ChatMessage, ChatRequest
from app.providers import DeferredRequestQueued, complete_chat
from app.queue import DeferredRequest, DeferredRequestWorker, enqueue_deferrable_request, idempotency_key_for


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    async def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def delete(self, name: str) -> int:
        existed = name in self.values
        self.values.pop(name, None)
        return int(existed)

    async def rpush(self, name: str, value: str) -> int:
        queue = self.lists.setdefault(name, [])
        queue.append(value)
        return len(queue)

    async def lpop(self, name: str) -> str | None:
        queue = self.lists.setdefault(name, [])
        if not queue:
            return None
        return queue.pop(0)

    async def llen(self, name: str) -> int:
        return len(self.lists.setdefault(name, []))

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        values = self.sorted_sets.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            added += int(member not in values)
            values[member] = score
        return added

    async def zrangebyscore(self, name: str, min_score: str | float, max_score: str | float) -> list[str]:
        min_value = float("-inf") if min_score == "-inf" else float(min_score)
        max_value = float(max_score)
        values = self.sorted_sets.setdefault(name, {})
        return [member for member, score in values.items() if min_value <= score <= max_value]

    async def zrem(self, name: str, member: str) -> int:
        values = self.sorted_sets.setdefault(name, {})
        existed = member in values
        values.pop(member, None)
        return int(existed)

    async def zcard(self, name: str) -> int:
        return len(self.sorted_sets.setdefault(name, {}))


class FakeHealthTracker:
    def __init__(self, health: ProviderHealth) -> None:
        self.health = health

    async def summary(self, provider: str, tenant: str, feature: str) -> ProviderHealth:
        return ProviderHealth(
            provider=provider,
            tenant=tenant,
            feature=feature,
            window_seconds=self.health.window_seconds,
            total=self.health.total,
            success=self.health.success,
            success_rate=self.health.success_rate,
            p50_latency_seconds=self.health.p50_latency_seconds,
            p95_latency_seconds=self.health.p95_latency_seconds,
            p99_latency_seconds=self.health.p99_latency_seconds,
            errors=self.health.errors,
        )

    async def record_success(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
    ) -> None:
        return None

    async def record_error(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
        error_type: str,
    ) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        redis_url="redis://localhost:6379/0",
        provider_preference="openai,anthropic",
        long_form_generation_provider_preference="openai,anthropic",
        deferrable_request_classes="long_form_generation",
        interactive_request_classes="classification",
        latency_sensitive_request_classes="",
        openai_api_key="test-openai-key",
        anthropic_api_key="test-anthropic-key",
        circuit_min_samples=2,
        circuit_error_rate_threshold=0.5,
        circuit_p95_latency_threshold_seconds=1.0,
        circuit_reset_seconds=5.0,
        circuit_half_open_probe_rate=1.0,
        queue_base_backoff_seconds=0.01,
        queue_jitter_seconds=0.0,
    )


def _health(success_rate: float) -> ProviderHealth:
    return ProviderHealth(
        provider="any",
        tenant="tenant-a",
        feature="long_form_generation",
        window_seconds=300,
        total=10,
        success=int(10 * success_rate),
        success_rate=success_rate,
        p50_latency_seconds=0.1,
        p95_latency_seconds=0.2,
        p99_latency_seconds=0.2,
        errors={
            "rate_limit": 0,
            "timeout": 10 - int(10 * success_rate),
            "server_error": 0,
            "content_filter": 0,
            "auth_failure": 0,
        },
    )


def _request() -> ChatRequest:
    return ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="write a long report")],
        metadata={"request_class": "long_form_generation"},
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
                    "message": {"role": "assistant", "content": f"processed by {self.provider}"},
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
                message=ChatCompletionMessage(role="assistant", content=f"processed by {provider}"),
                finish_reason="stop",
            )
        ],
    )


async def _submit_deferrable_request(
    settings: Settings,
    queue_redis: FakeRedis,
    tracker: FakeHealthTracker,
    breaker: ProviderCircuitBreaker,
) -> ChatCompletionResponse:
    return await complete_chat(
        request=_request(),
        settings=settings,
        tenant="tenant-a",
        feature="long_form_generation",
        request_id="req-deferrable-1",
        health_tracker=tracker,
        breaker=breaker,
        queue_redis=queue_redis,
    )


def test_deferrable_request_queues_when_all_providers_open_and_worker_processes_it(monkeypatch) -> None:
    settings = _settings()
    queue_redis = FakeRedis()
    tracker = FakeHealthTracker(_health(success_rate=0.0))
    now = 0.0
    processed: list[str] = []

    async def fake_acompletion(**payload):
        provider = _provider_for_model(settings, payload["model"])
        processed.append(provider)
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

    try:
        asyncio.run(_submit_deferrable_request(settings, queue_redis, tracker, breaker))
    except DeferredRequestQueued as queued:
        assert queued.queue_name == settings.queue_name
    else:
        raise AssertionError("deferrable request was not queued")

    assert len(queue_redis.lists[settings.queue_name]) == 1
    queued_payload = json.loads(queue_redis.lists[settings.queue_name][0])
    assert queued_payload["request_id"] == "req-deferrable-1"
    assert queued_payload["attempts"] == 0
    assert processed == []

    tracker.health = _health(success_rate=1.0)
    now = 6.0

    async def process_deferred(item: DeferredRequest) -> None:
        await complete_chat(
            request=item.request,
            settings=settings,
            tenant=item.tenant,
            feature=item.feature,
            request_id=item.request_id,
            health_tracker=tracker,
            breaker=breaker,
            queue_redis=queue_redis,
            allow_queue=False,
        )

    worker = DeferredRequestWorker(
        redis=queue_redis,
        settings=settings,
        process_request=process_deferred,
        random_fn=lambda: 0.0,
        clock=lambda: now,
    )

    assert asyncio.run(worker.process_once()) is True
    assert queue_redis.lists[settings.queue_name] == []
    assert processed == ["openai"]
    assert queue_redis.values[f"{settings.queue_idempotency_prefix}:completed:{queued_payload['idempotency_key']}"] == "completed"


def test_enqueue_deferrable_request_is_idempotent_by_request_id() -> None:
    settings = _settings()
    queue_redis = FakeRedis()
    request = _request()

    first = asyncio.run(
        enqueue_deferrable_request(
            redis=queue_redis,
            settings=settings,
            request=request,
            tenant="tenant-a",
            feature="long_form_generation",
            request_id="same-request",
        )
    )
    second = asyncio.run(
        enqueue_deferrable_request(
            redis=queue_redis,
            settings=settings,
            request=request,
            tenant="tenant-a",
            feature="long_form_generation",
            request_id="same-request",
        )
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key == idempotency_key_for("tenant-a", "long_form_generation", "same-request")
    assert len(queue_redis.lists[settings.queue_name]) == 1


def test_worker_retries_with_exponential_backoff_and_jitter_then_completes_once() -> None:
    settings = _settings()
    queue_redis = FakeRedis()
    now = 100.0
    request = _request()
    item = asyncio.run(
        enqueue_deferrable_request(
            redis=queue_redis,
            settings=settings,
            request=request,
            tenant="tenant-a",
            feature="long_form_generation",
            request_id="retry-me",
        )
    )
    calls: list[int] = []

    async def process(item: DeferredRequest) -> None:
        calls.append(item.attempts)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")

    worker = DeferredRequestWorker(
        redis=queue_redis,
        settings=settings,
        process_request=process,
        random_fn=lambda: 0.5,
        clock=lambda: now,
    )

    assert asyncio.run(worker.process_once()) is True
    assert calls == [0]
    assert queue_redis.lists[settings.queue_name] == []
    delayed_payload, run_at = next(iter(queue_redis.sorted_sets[settings.queue_delayed_name].items()))
    expected_delay = settings.queue_base_backoff_seconds + (0.5 * settings.queue_jitter_seconds)
    assert run_at == now + expected_delay
    assert DeferredRequest.from_json(delayed_payload).attempts == 1

    assert asyncio.run(worker.process_once()) is False
    assert calls == [0]

    now = run_at
    assert asyncio.run(worker.process_once()) is True
    assert calls == [0, 1]
    assert queue_redis.values[f"{settings.queue_idempotency_prefix}:completed:{item.idempotency_key}"] == "completed"

    asyncio.run(queue_redis.rpush(settings.queue_name, item.to_json()))
    assert asyncio.run(worker.process_once()) is True
    assert calls == [0, 1]


def test_worker_marks_item_failed_after_max_attempts_without_duplicate_processing() -> None:
    settings = Settings(
        redis_url="redis://localhost:6379/0",
        provider_preference="openai,anthropic",
        long_form_generation_provider_preference="openai,anthropic",
        deferrable_request_classes="long_form_generation",
        openai_api_key="test-openai-key",
        anthropic_api_key="test-anthropic-key",
        queue_base_backoff_seconds=0.01,
        queue_jitter_seconds=0.0,
        queue_max_attempts=1,
    )
    queue_redis = FakeRedis()
    item = asyncio.run(
        enqueue_deferrable_request(
            redis=queue_redis,
            settings=settings,
            request=_request(),
            tenant="tenant-a",
            feature="long_form_generation",
            request_id="fail-once",
        )
    )

    async def fail(item: DeferredRequest) -> None:
        raise RuntimeError("permanent failure")

    worker = DeferredRequestWorker(redis=queue_redis, settings=settings, process_request=fail)

    assert asyncio.run(worker.process_once()) is True
    assert queue_redis.values[f"{settings.queue_idempotency_prefix}:failed:{item.idempotency_key}"] == "failed"
    assert queue_redis.sorted_sets.get(settings.queue_delayed_name, {}) == {}
