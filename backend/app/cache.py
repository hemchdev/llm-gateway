import json

from redis.asyncio import Redis

from .config import Settings
from .models import ChatCompletionResponse, ChatRequest
from .privacy import prompt_hash


class SemanticCache:
    def __init__(self, redis: Redis, key_prefix: str, ttl_seconds: int) -> None:
        self.redis = redis
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_settings(cls, redis: Redis, settings: Settings) -> "SemanticCache":
        return cls(
            redis=redis,
            key_prefix=settings.semantic_cache_key_prefix,
            ttl_seconds=settings.semantic_cache_ttl_seconds,
        )

    async def get(self, request: ChatRequest) -> ChatCompletionResponse | None:
        payload = await self.redis.get(self.key_for(request))
        if payload is None:
            return None
        return ChatCompletionResponse.model_validate_json(payload)

    async def set(self, request: ChatRequest, response: ChatCompletionResponse) -> None:
        await self.redis.set(
            self.key_for(request),
            json.dumps(response.model_dump(mode="json"), sort_keys=True),
            ex=self.ttl_seconds,
        )

    def key_for(self, request: ChatRequest) -> str:
        return f"{self.key_prefix}:{request.model}:{prompt_hash(request.messages)}"
