import asyncio

from app.cache import SemanticCache
from app.config import Settings
from app.models import ChatCompletionChoice, ChatCompletionMessage, ChatCompletionResponse, ChatMessage, ChatRequest
from app.privacy import normalize_prompt, prompt_hash


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True


def _request(content: str) -> ChatRequest:
    return ChatRequest(model="gpt-4o-mini", messages=[ChatMessage(role="user", content=content)])


def _response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-cache-test",
        created=1,
        model="gpt-4o-mini",
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(content="cached"),
                finish_reason="stop",
            )
        ],
    )


def test_prompt_hash_normalizes_case_whitespace_and_redacts_pii() -> None:
    first = _request(" Email me at Alice@example.com   please ")
    second = _request("email me at [REDACTED_EMAIL] please")

    assert normalize_prompt(first.messages) == normalize_prompt(second.messages)
    assert prompt_hash(first.messages) == prompt_hash(second.messages)


def test_semantic_cache_round_trips_response_by_normalized_prompt_hash() -> None:
    settings = Settings(semantic_cache_ttl_seconds=123)
    redis = FakeRedis()
    cache = SemanticCache.from_settings(redis, settings)
    request = _request("Hello cache")
    response = _response()

    asyncio.run(cache.set(request, response))
    cached = asyncio.run(cache.get(_request(" hello   CACHE ")))

    assert cached == response
    assert redis.expirations[cache.key_for(request)] == 123
