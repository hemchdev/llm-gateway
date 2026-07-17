import time
from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis

from .config import Settings
from .models import ChatRequest


@dataclass(frozen=True)
class TenantLimitDecision:
    allowed: bool
    reason: str | None = None


class TenantLimiter:
    def __init__(self, redis: Redis, settings: Settings, clock=time.time) -> None:
        self.redis = redis
        self.settings = settings
        self.clock = clock

    async def check_and_consume(
        self,
        tenant: str,
        feature: str,
        request: ChatRequest,
    ) -> TenantLimitDecision:
        rate_key = f"{self.settings.tenant_limit_key_prefix}:rate:{tenant}:{feature}"
        count = await self.redis.incr(rate_key)
        if count == 1:
            await self.redis.expire(rate_key, self.settings.tenant_rate_limit_window_seconds)
        if count > self.settings.tenant_rate_limit_per_window:
            return TenantLimitDecision(allowed=False, reason="rate_limit")

        estimated_cost = estimate_request_cost(request, self.settings)
        budget_key = f"{self.settings.tenant_limit_key_prefix}:budget:{self._month_key()}:{tenant}:{feature}"
        current_spend = float(await self.redis.get(budget_key) or 0.0)
        if current_spend + estimated_cost > self.settings.tenant_monthly_budget_usd:
            return TenantLimitDecision(allowed=False, reason="monthly_budget")

        await self.redis.incrbyfloat(budget_key, estimated_cost)
        await self.redis.expire(budget_key, self.settings.tenant_budget_key_ttl_seconds)
        return TenantLimitDecision(allowed=True)

    def _month_key(self) -> str:
        return time.strftime("%Y-%m", time.gmtime(self.clock()))


def estimate_request_cost(request: ChatRequest, settings: Settings) -> float:
    prompt_tokens = sum(len(str(message.content).split()) for message in request.messages)
    max_tokens = request.max_tokens or settings.default_completion_token_estimate
    return ((prompt_tokens + max_tokens) / 1000) * settings.estimated_cost_per_1k_tokens


async def enforce_tenant_limits(
    limiter: TenantLimiter,
    tenant: str,
    feature: str,
    request: ChatRequest,
) -> None:
    decision = await limiter.check_and_consume(tenant, feature, request)
    if decision.allowed:
        return
    if decision.reason == "rate_limit":
        raise HTTPException(status_code=429, detail="Tenant rate limit exceeded")
    raise HTTPException(status_code=402, detail="Tenant monthly budget exceeded")
