from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeRedis:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable

    async def ping(self) -> bool:
        if not self.reachable:
            raise ConnectionError("redis unavailable")
        return True


@dataclass
class FakeCircuitState:
    value: str


class FakeBreaker:
    def __init__(self, state: str = "closed") -> None:
        self.state = state

    def state_for(self, provider: str) -> FakeCircuitState:
        return FakeCircuitState(self.state)


def _settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "admin_api_key": "test-admin-token",
        "metrics_api_key": "test-metrics-token",
        "allowed_origins": "https://app.example.test",
        "max_request_body_bytes": 1024,
    }
    values.update(overrides)
    return Settings(**values)


def _client(settings: Settings | None = None) -> TestClient:
    app = create_app(settings or _settings())
    app.state.queue_redis = FakeRedis()
    app.state.provider_breaker = FakeBreaker()
    return TestClient(app)


def test_missing_gateway_headers_use_consistent_error_shape() -> None:
    response = _client().post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": []})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "missing_required_headers",
            "message": "Missing required headers: x-tenant-id, x-feature, x-request-id",
            "request_id": "unknown",
        }
    }


def test_request_body_size_limit_rejects_large_requests() -> None:
    client = _client(_settings(max_request_body_bytes=1024))

    response = client.post(
        "/v1/chat/completions",
        content="x" * 2048,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-Id": "tenant-a",
            "X-Feature": "classification",
            "X-Request-Id": "req-large",
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.json()["error"]["request_id"] == "req-large"


def test_metrics_requires_bearer_token() -> None:
    client = _client()

    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer test-metrics-token"}).status_code == 200


def test_admin_chaos_requires_bearer_token() -> None:
    response = _client().post(
        "/admin/chaos",
        json={"provider": "openai", "duration_seconds": 10, "rate": 1.0, "error_type": "server_error"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_ready_reflects_redis_connectivity() -> None:
    app = create_app(_settings())
    app.state.queue_redis = FakeRedis(reachable=True)
    app.state.provider_breaker = FakeBreaker("closed")
    client = TestClient(app)

    assert client.get("/ready").status_code == 200

    app.state.queue_redis = FakeRedis(reachable=False)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_ready_requires_at_least_one_non_open_provider_circuit() -> None:
    app = create_app(_settings())
    app.state.queue_redis = FakeRedis(reachable=True)
    app.state.provider_breaker = FakeBreaker("open")

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_production_startup_validation_fails_for_missing_required_secrets() -> None:
    with pytest.raises(RuntimeError) as exc:
        create_app(
            Settings(
                environment="prod",
                allowed_origins="*",
                provider_preference="openai",
                classification_provider_preference="openai",
                long_form_generation_provider_preference="openai",
                require_provider_api_keys=True,
                openai_api_key="",
            )
        )

    assert "LLM_GATEWAY_ALLOWED_ORIGINS" in str(exc.value)
    assert "LLM_GATEWAY_ADMIN_API_KEY" in str(exc.value)
    assert "LLM_GATEWAY_METRICS_API_KEY" in str(exc.value)
    assert "missing API keys" in str(exc.value)
