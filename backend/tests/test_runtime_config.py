from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import ChatMessage, ChatRequest
from app.providers import _runtime_available_routes
from app.runtime_config import ProviderStore


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, name: str, value: str) -> bool:
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def delete(self, name: str) -> int:
        existed = name in self.values
        self.values.pop(name, None)
        return int(existed)

    async def sadd(self, name: str, *values: str) -> int:
        members = self.sets.setdefault(name, set())
        before = len(members)
        members.update(values)
        return len(members) - before

    async def smembers(self, name: str) -> set[str]:
        return set(self.sets.get(name, set()))

    async def srem(self, name: str, *values: str) -> int:
        members = self.sets.setdefault(name, set())
        removed = 0
        for value in values:
            if value in members:
                members.remove(value)
                removed += 1
        return removed


def _settings(encryption_key: str) -> Settings:
    return Settings(
        environment="test",
        encryption_key=encryption_key,
        provider_preference="",
        classification_provider_preference="",
        long_form_generation_provider_preference="",
    )


def test_provider_store_encrypts_api_key_and_returns_masked_display_values() -> None:
    encryption_key = Fernet.generate_key().decode("utf-8")
    redis = FakeRedis()
    store = ProviderStore(redis, encryption_key=encryption_key)

    created = asyncio.run(
        store.create(
            {
                "name": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-test-secret-value",
                "api_base": "https://api.openai.com/v1",
                "request_classes": ["classification"],
                "priority": 10,
            }
        )
    )

    stored_payload = next(iter(redis.values.values()))
    assert "sk-test-secret-value" not in stored_payload
    assert "encrypted_api_key" in stored_payload
    assert created["api_key"] == "sk-t...alue"
    assert created["has_api_key"] is True

    listed = asyncio.run(store.list())
    assert listed[0]["api_key"] == "sk-t...alue"

    raw = asyncio.run(store.get_raw("openai"))
    assert raw is not None
    assert raw["api_key"] == "sk-test-secret-value"
    assert raw["api_base"] == "https://api.openai.com/v1"


def test_runtime_routes_decrypt_keys_for_provider_calls() -> None:
    encryption_key = Fernet.generate_key().decode("utf-8")
    redis = FakeRedis()
    settings = _settings(encryption_key)
    store = ProviderStore.from_settings(redis, settings)
    asyncio.run(
        store.create(
            name="local-openai",
            model="openai/local-model",
            api_key="local-secret",
            api_base="http://localhost:11434/v1",
            request_classes="classification",
            priority=1,
        )
    )
    request = ChatRequest(
        model="ignored-by-runtime-store",
        messages=[ChatMessage(role="user", content="hello")],
    )

    routes = asyncio.run(_runtime_available_routes(settings, request, "classification", redis))

    assert len(routes) == 1
    assert routes[0].name == "local-openai"
    assert routes[0].model == "openai/local-model"
    assert routes[0].api_key == "local-secret"
    assert routes[0].api_base == "http://localhost:11434/v1"


def test_provider_store_updates_deletes_and_orders_by_priority() -> None:
    encryption_key = Fernet.generate_key().decode("utf-8")
    redis = FakeRedis()
    store = ProviderStore(redis, encryption_key=encryption_key)

    asyncio.run(store.create(name="slow", model="model-slow", api_key="slow-key", priority=20))
    asyncio.run(store.create(name="fast", model="model-fast", api_key="fast-key", priority=5))
    asyncio.run(store.update("slow", {"priority": 1}))

    ordered = asyncio.run(store.ordered_for_class("classification"))
    assert [provider["name"] for provider in ordered] == ["slow", "fast"]

    assert asyncio.run(store.delete("slow")) is True
    assert asyncio.run(store.get_raw("slow")) is None


def test_admin_provider_api_stores_custom_openai_compatible_provider_in_redis() -> None:
    encryption_key = Fernet.generate_key().decode("utf-8")
    redis = FakeRedis()
    app = create_app(
        Settings(
            environment="test",
            encryption_key=encryption_key,
            admin_api_key="admin-token",
            metrics_api_key="metrics-token",
            allowed_origins="http://localhost:3001",
        )
    )
    app.state.queue_redis = redis
    client = TestClient(app)

    payload = {
        "name": "personal-inference",
        "model": "openai/personal-model",
        "api_key": "personal-secret",
        "api_base": "https://inference.example.com/v1",
        "request_classes": ["classification"],
        "priority": 1,
        "enabled": True,
    }

    assert client.post("/admin/providers", json=payload).status_code == 403
    created = client.post("/admin/providers", json=payload, headers={"X-Admin-Key": "admin-token"})

    assert created.status_code == 200
    assert created.json()["name"] == "personal-inference"
    assert created.json()["api_key"] == "pers...cret"
    assert "personal-secret" not in next(iter(redis.values.values()))

    listed = client.get("/admin/providers", headers={"X-Admin-Key": "admin-token"})
    assert listed.status_code == 200
    assert listed.json()["providers"][0]["api_base"] == "https://inference.example.com/v1"

    deleted = client.delete("/admin/providers/personal-inference", headers={"X-Admin-Key": "admin-token"})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
