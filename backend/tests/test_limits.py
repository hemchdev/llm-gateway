import asyncio

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.limits import TenantLimiter, enforce_tenant_limits, estimate_request_cost
from app.models import ChatMessage, ChatRequest


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def incrbyfloat(self, key: str, amount: float) -> float:
        value = float(self.values.get(key, "0")) + amount
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


def _request() -> ChatRequest:
    return ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hello world")],
        max_tokens=10,
    )


def test_tenant_limiter_rejects_rate_limit_over_window_count() -> None:
    settings = Settings(tenant_rate_limit_per_window=1)
    limiter = TenantLimiter(FakeRedis(), settings, clock=lambda: 1_784_000_000)

    first = asyncio.run(limiter.check_and_consume("tenant-a", "classification", _request()))
    second = asyncio.run(limiter.check_and_consume("tenant-a", "classification", _request()))

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "rate_limit"


def test_tenant_limiter_rejects_monthly_budget() -> None:
    settings = Settings(
        estimated_cost_per_1k_tokens=1.0,
        tenant_monthly_budget_usd=0.005,
        tenant_rate_limit_per_window=10,
    )
    limiter = TenantLimiter(FakeRedis(), settings, clock=lambda: 1_784_000_000)

    decision = asyncio.run(limiter.check_and_consume("tenant-a", "classification", _request()))

    assert decision.allowed is False
    assert decision.reason == "monthly_budget"


def test_estimate_request_cost_uses_prompt_and_max_tokens() -> None:
    settings = Settings(estimated_cost_per_1k_tokens=2.0)

    assert estimate_request_cost(_request(), settings) == 0.024


def test_enforce_tenant_limits_raises_http_errors() -> None:
    settings = Settings(tenant_rate_limit_per_window=1)
    limiter = TenantLimiter(FakeRedis(), settings, clock=lambda: 1_784_000_000)
    asyncio.run(enforce_tenant_limits(limiter, "tenant-a", "classification", _request()))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(enforce_tenant_limits(limiter, "tenant-a", "classification", _request()))

    assert exc.value.status_code == 429
