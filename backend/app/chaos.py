import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from .models import ErrorResponse


ChaosErrorType = Literal["rate_limit", "timeout", "server_error", "content_filter", "auth_failure"]


class ChaosInjectionRequest(BaseModel):
    provider: str
    duration_seconds: float = Field(..., gt=0)
    rate: float = Field(..., ge=0.0, le=1.0)
    error_type: ChaosErrorType | None = None
    latency_ms: int = Field(default=0, ge=0)


class ChaosInjectionResponse(BaseModel):
    provider: str
    active_until: float
    rate: float
    error_type: ChaosErrorType | None
    latency_ms: int


@dataclass(frozen=True)
class ChaosRule:
    provider: str
    active_until: float
    rate: float
    error_type: str | None
    latency_ms: int


class SyntheticProviderError(Exception):
    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(f"Synthetic chaos error: {error_type}")


class ChaosController:
    def __init__(
        self,
        redis: Redis,
        key_prefix: str,
        random_fn=random.random,
        clock=time.time,
        sleep=asyncio.sleep,
    ) -> None:
        self.redis = redis
        self.key_prefix = key_prefix
        self.random_fn = random_fn
        self.clock = clock
        self.sleep = sleep

    async def inject(
        self,
        provider: str,
        duration_seconds: float,
        rate: float,
        error_type: str | None,
        latency_ms: int,
    ) -> ChaosRule:
        rule = ChaosRule(
            provider=provider,
            active_until=self.clock() + duration_seconds,
            rate=rate,
            error_type=error_type,
            latency_ms=latency_ms,
        )
        await self.redis.set(self._key(provider), json.dumps(rule.__dict__, sort_keys=True), ex=int(duration_seconds))
        return rule

    async def apply(self, provider: str) -> None:
        payload = await self.redis.get(self._key(provider))
        if payload is None:
            return
        data = json.loads(payload)
        rule = ChaosRule(**data)
        if rule.active_until < self.clock():
            await self.redis.delete(self._key(provider))
            return
        if self.random_fn() > rule.rate:
            return
        if rule.latency_ms:
            await self.sleep(rule.latency_ms / 1000)
        if rule.error_type:
            raise SyntheticProviderError(rule.error_type)

    def _key(self, provider: str) -> str:
        return f"{self.key_prefix}:{provider}"


router = APIRouter(prefix="/admin")


@router.post(
    "/chaos",
    response_model=ChaosInjectionResponse,
    tags=["admin"],
    summary="Inject provider chaos",
    description=(
        "Adds temporary synthetic latency and/or errors for one provider. "
        "Use this to demonstrate circuit breaking, failover, and recovery behavior."
    ),
    responses={
        200: {
            "description": "The chaos rule was stored and will be applied to matching provider calls.",
            "content": {
                "application/json": {
                    "example": {
                        "provider": "openai",
                        "active_until": 1784999733.7655878,
                        "rate": 1.0,
                        "error_type": "server_error",
                        "latency_ms": 250,
                    }
                }
            },
        },
        401: {
            "model": ErrorResponse,
            "description": "Missing admin authentication.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {"code": "unauthorized", "message": "Unauthorized", "request_id": "unknown"}
                    }
                }
            },
        },
        403: {
            "model": ErrorResponse,
            "description": "Invalid admin authentication.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {"code": "forbidden", "message": "Forbidden", "request_id": "unknown"}
                    }
                }
            },
        },
        422: {"model": ErrorResponse, "description": "The chaos rule payload is invalid."},
        503: {"model": ErrorResponse, "description": "Admin authentication or Redis is unavailable."},
    },
)
async def inject_chaos(
    request: Request,
    payload: ChaosInjectionRequest = Body(
        ...,
        examples=[
            {
                "provider": "openai",
                "duration_seconds": 60,
                "rate": 1.0,
                "error_type": "server_error",
                "latency_ms": 250,
            }
        ],
        openapi_examples={
            "provider_degradation": {
                "summary": "Inject provider degradation",
                "description": "Force OpenAI calls to fail for one minute so failover and circuit breaking can be observed.",
                "value": {
                    "provider": "openai",
                    "duration_seconds": 60,
                    "rate": 1.0,
                    "error_type": "server_error",
                    "latency_ms": 250,
                },
            }
        },
    ),
) -> ChaosInjectionResponse:
    controller: ChaosController | None = getattr(request.app.state, "chaos_controller", None)
    if controller is None:
        raise HTTPException(status_code=500, detail="Chaos controller is not configured")
    rule = await controller.inject(
        provider=payload.provider,
        duration_seconds=payload.duration_seconds,
        rate=payload.rate,
        error_type=payload.error_type,
        latency_ms=payload.latency_ms,
    )
    return ChaosInjectionResponse(
        provider=rule.provider,
        active_until=rule.active_until,
        rate=rule.rate,
        error_type=rule.error_type,
        latency_ms=rule.latency_ms,
    )
