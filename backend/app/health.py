import json
import math
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis

from .config import Settings, get_settings
from .models import ErrorResponse, HealthResponse, ReadyResponse

router = APIRouter()

ERROR_TYPES = ("rate_limit", "timeout", "server_error", "content_filter", "auth_failure")


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    tenant: str
    feature: str
    window_seconds: int
    total: int
    success: int
    success_rate: float
    p50_latency_seconds: float | None
    p95_latency_seconds: float | None
    p99_latency_seconds: float | None
    errors: dict[str, int] = field(default_factory=dict)


class HealthTracker:
    """Records provider outcomes in Redis sorted sets scored by event timestamp."""

    def __init__(
        self,
        redis: Redis,
        window_seconds: int = 300,
        key_prefix: str = "llm_gateway:health",
    ) -> None:
        self.redis = redis
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix

    @classmethod
    def from_settings(cls, settings: Settings) -> "HealthTracker":
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        return cls(
            redis=redis,
            window_seconds=settings.health_window_seconds,
            key_prefix=settings.health_redis_key_prefix,
        )

    async def record_success(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
    ) -> None:
        await self._record(
            provider=provider,
            tenant=tenant,
            feature=feature,
            status="success",
            latency_seconds=latency_seconds,
            request_id=request_id,
            error_type=None,
        )

    async def record_error(
        self,
        provider: str,
        tenant: str,
        feature: str,
        latency_seconds: float,
        request_id: str,
        error_type: str,
    ) -> None:
        await self._record(
            provider=provider,
            tenant=tenant,
            feature=feature,
            status="error",
            latency_seconds=latency_seconds,
            request_id=request_id,
            error_type=error_type,
        )

    async def summary(
        self,
        provider: str,
        tenant: str,
        feature: str,
        now: float | None = None,
    ) -> ProviderHealth:
        if now is None:
            now = time.time()
        key = self._key(provider, tenant, feature)
        await self._prune(key, now)
        raw_events = await self.redis.zrangebyscore(key, now - self.window_seconds, now)
        events = [json.loads(event) for event in raw_events]
        return self.calculate(provider, tenant, feature, self.window_seconds, events)

    @staticmethod
    def calculate(
        provider: str,
        tenant: str,
        feature: str,
        window_seconds: int,
        events: list[dict[str, Any]],
    ) -> ProviderHealth:
        total = len(events)
        successes = sum(1 for event in events if event["status"] == "success")
        latencies = sorted(float(event["latency_seconds"]) for event in events)
        errors = {error_type: 0 for error_type in ERROR_TYPES}
        for event in events:
            error_type = event.get("error_type")
            if error_type in errors:
                errors[error_type] += 1

        return ProviderHealth(
            provider=provider,
            tenant=tenant,
            feature=feature,
            window_seconds=window_seconds,
            total=total,
            success=successes,
            success_rate=successes / total if total else 0.0,
            p50_latency_seconds=_percentile(latencies, 50),
            p95_latency_seconds=_percentile(latencies, 95),
            p99_latency_seconds=_percentile(latencies, 99),
            errors=errors,
        )

    async def _record(
        self,
        provider: str,
        tenant: str,
        feature: str,
        status: str,
        latency_seconds: float,
        request_id: str,
        error_type: str | None,
    ) -> None:
        now = time.time()
        key = self._key(provider, tenant, feature)
        event = {
            "id": f"{now}:{request_id}:{uuid4().hex}",
            "provider": provider,
            "tenant": tenant,
            "feature": feature,
            "status": status,
            "latency_seconds": latency_seconds,
            "error_type": error_type,
            "request_id": request_id,
            "timestamp": now,
        }
        await self.redis.zadd(key, {json.dumps(event, sort_keys=True): now})
        await self._prune(key, now)

    async def _prune(self, key: str, now: float) -> None:
        await self.redis.zremrangebyscore(key, "-inf", f"({now - self.window_seconds}")

    def _key(self, provider: str, tenant: str, feature: str) -> str:
        return f"{self.key_prefix}:{provider}:{tenant}:{feature}:events"


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]

    rank = (percentile / 100) * (len(values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    weight = rank - lower
    return values[lower] + (values[upper] - values[lower]) * weight


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Check liveness",
    description="Returns a lightweight liveness signal when the API process is running.",
    responses={
        200: {
            "description": "The API process is alive.",
            "content": {
                "application/json": {
                    "example": {"status": "ok", "service": "LLM Gateway", "environment": "prod"}
                }
            },
        }
    },
)
async def health(request: Request, settings: Settings = Depends(get_settings)) -> HealthResponse:
    settings = getattr(request.app.state, "settings", settings)
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    tags=["health"],
    summary="Check readiness",
    description=(
        "Checks whether Redis is reachable and at least one configured provider circuit is not open. "
        "Use this endpoint for load balancer or orchestrator readiness probes."
    ),
    responses={
        200: {
            "description": "The API is ready to accept traffic.",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ready",
                        "service": "LLM Gateway",
                        "environment": "prod",
                        "providers": ["openai", "anthropic"],
                    }
                }
            },
        },
        503: {
            "model": ErrorResponse,
            "description": "Redis is unavailable or all provider circuits are open.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "service_unavailable",
                            "message": "Service temporarily unavailable",
                            "request_id": "unknown",
                        }
                    }
                }
            },
        },
    },
)
async def ready(request: Request, settings: Settings = Depends(get_settings)) -> ReadyResponse:
    settings = getattr(request.app.state, "settings", settings)
    redis = getattr(request.app.state, "queue_redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis client is not configured")

    try:
        await redis.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Redis is not reachable") from exc

    breaker = getattr(request.app.state, "provider_breaker", None)
    if breaker is None:
        raise HTTPException(status_code=503, detail="Circuit breaker is not configured")

    providers = settings.configured_provider_names
    available_providers = [
        provider
        for provider in providers
        if getattr(breaker.state_for(provider), "value", breaker.state_for(provider)) != "open"
    ]
    if not available_providers:
        raise HTTPException(status_code=503, detail="No provider circuit is ready")

    return ReadyResponse(
        status="ready",
        service=settings.app_name,
        environment=settings.environment,
        providers=available_providers,
    )
