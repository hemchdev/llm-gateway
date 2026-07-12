import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from .config import Settings
from .metrics import set_queue_depth
from .models import ChatRequest
from .privacy import redacted_request


@dataclass(frozen=True)
class DeferredRequest:
    idempotency_key: str
    tenant: str
    feature: str
    request_id: str
    request: ChatRequest
    attempts: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "idempotency_key": self.idempotency_key,
                "tenant": self.tenant,
                "feature": self.feature,
                "request_id": self.request_id,
                "request": self.request.model_dump(mode="json"),
                "attempts": self.attempts,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, payload: str) -> "DeferredRequest":
        data = json.loads(payload)
        return cls(
            idempotency_key=data["idempotency_key"],
            tenant=data["tenant"],
            feature=data["feature"],
            request_id=data["request_id"],
            request=ChatRequest.model_validate(data["request"]),
            attempts=int(data.get("attempts", 0)),
        )

    def next_attempt(self) -> "DeferredRequest":
        return DeferredRequest(
            idempotency_key=self.idempotency_key,
            tenant=self.tenant,
            feature=self.feature,
            request_id=self.request_id,
            request=self.request,
            attempts=self.attempts + 1,
        )


ProcessDeferredRequest = Callable[[DeferredRequest], Awaitable[None]]


def get_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def idempotency_key_for(tenant: str, feature: str, request_id: str) -> str:
    raw_key = f"{tenant}:{feature}:{request_id}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def enqueue_deferrable_request(
    redis: Redis,
    settings: Settings,
    request: ChatRequest,
    tenant: str,
    feature: str,
    request_id: str,
) -> DeferredRequest:
    item = DeferredRequest(
        idempotency_key=idempotency_key_for(tenant, feature, request_id),
        tenant=tenant,
        feature=feature,
        request_id=request_id,
        request=redacted_request(request),
    )
    queued_key = _queued_key(settings, item.idempotency_key)
    was_set = await redis.set(queued_key, "queued", nx=True)
    if was_set:
        await redis.rpush(settings.queue_name, item.to_json())
        await refresh_queue_depth(redis, settings)
    return item


class DeferredRequestWorker:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        process_request: ProcessDeferredRequest,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fn: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.process_request = process_request
        self.sleep = sleep
        self.random_fn = random_fn
        self.clock = clock

    async def run(self, poll_interval_seconds: float = 1.0) -> None:
        while True:
            processed = await self.process_once()
            if not processed:
                await self.sleep(poll_interval_seconds)

    async def process_once(self) -> bool:
        await self._move_due_delayed_items()
        payload = await self.redis.lpop(self.settings.queue_name)
        await refresh_queue_depth(self.redis, self.settings)
        if payload is None:
            return False

        item = DeferredRequest.from_json(payload)
        if await self.redis.get(_completed_key(self.settings, item.idempotency_key)):
            return True

        processing_key = _processing_key(self.settings, item.idempotency_key)
        if not await self.redis.set(processing_key, "processing", nx=True, ex=60):
            return True

        try:
            await self.process_request(item)
        except Exception:
            await self.redis.delete(processing_key)
            await self._retry_or_fail(item)
            return True

        await self.redis.set(_completed_key(self.settings, item.idempotency_key), "completed")
        await self.redis.delete(processing_key)
        return True

    async def _retry_or_fail(self, item: DeferredRequest) -> None:
        if item.attempts + 1 >= self.settings.queue_max_attempts:
            await self.redis.set(_failed_key(self.settings, item.idempotency_key), "failed")
            return

        retry = item.next_attempt()
        delay = min(
            self.settings.queue_max_backoff_seconds,
            self.settings.queue_base_backoff_seconds * (2**item.attempts),
        )
        delay += self.random_fn() * self.settings.queue_jitter_seconds
        await self.redis.zadd(self.settings.queue_delayed_name, {retry.to_json(): self.clock() + delay})
        await refresh_queue_depth(self.redis, self.settings)

    async def _move_due_delayed_items(self) -> None:
        now = self.clock()
        due_items = await self.redis.zrangebyscore(self.settings.queue_delayed_name, "-inf", now)
        for payload in due_items:
            removed = await self.redis.zrem(self.settings.queue_delayed_name, payload)
            if removed:
                await self.redis.rpush(self.settings.queue_name, payload)
        await refresh_queue_depth(self.redis, self.settings)


async def refresh_queue_depth(redis: Redis, settings: Settings) -> None:
    ready_depth = await redis.llen(settings.queue_name)
    delayed_depth = await redis.zcard(settings.queue_delayed_name)
    set_queue_depth(settings.queue_name, ready_depth)
    set_queue_depth(settings.queue_delayed_name, delayed_depth)


def _queued_key(settings: Settings, idempotency_key: str) -> str:
    return f"{settings.queue_idempotency_prefix}:queued:{idempotency_key}"


def _processing_key(settings: Settings, idempotency_key: str) -> str:
    return f"{settings.queue_idempotency_prefix}:processing:{idempotency_key}"


def _completed_key(settings: Settings, idempotency_key: str) -> str:
    return f"{settings.queue_idempotency_prefix}:completed:{idempotency_key}"


def _failed_key(settings: Settings, idempotency_key: str) -> str:
    return f"{settings.queue_idempotency_prefix}:failed:{idempotency_key}"
