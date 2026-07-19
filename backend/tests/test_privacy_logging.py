import asyncio
import logging
from dataclasses import dataclass

from app.config import Settings
from app.logging_utils import log_request_received
from app.models import ChatMessage, ChatRequest
from app.queue import DeferredRequest, enqueue_deferrable_request


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.queue: list[str] = []

    async def set(self, key: str, value: str, nx: bool = False) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def rpush(self, key: str, value: str) -> int:
        self.queue.append(value)
        return len(self.queue)

    async def llen(self, key: str) -> int:
        return len(self.queue)

    async def zcard(self, key: str) -> int:
        return 0


@dataclass
class LogRecordCapture:
    message: str = ""
    fields: dict | None = None


class CaptureHandler(logging.Handler):
    def __init__(self, capture: LogRecordCapture) -> None:
        super().__init__()
        self.capture = capture

    def emit(self, record: logging.LogRecord) -> None:
        self.capture.message = record.getMessage()
        self.capture.fields = getattr(record, "llm_gateway_fields", None)


def _request() -> ChatRequest:
    return ChatRequest(
        model="gpt-4o-mini",
        messages=[
            ChatMessage(
                role="user",
                content="Email alice@example.com or call 555-123-4567. SSN 123-45-6789.",
            )
        ],
        metadata={"owner": "bob@example.com"},
    )


def test_request_logging_redacts_pii_before_emitting_log() -> None:
    capture = LogRecordCapture()
    handler = CaptureHandler(capture)
    logger = logging.getLogger("llm_gateway.requests")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        log_request_received("tenant-a", "classification", "req-1", _request())
    finally:
        logger.removeHandler(handler)

    assert "alice@example.com" not in capture.message
    assert "555-123-4567" not in capture.message
    assert "123-45-6789" not in capture.message
    assert capture.fields is not None
    serialized_fields = str(capture.fields)
    assert "alice@example.com" not in serialized_fields
    assert "555-123-4567" not in serialized_fields
    assert "123-45-6789" not in serialized_fields
    assert "[REDACTED_EMAIL]" in serialized_fields
    assert "[REDACTED_PHONE]" in serialized_fields
    assert "[REDACTED_SSN]" in serialized_fields


def test_queue_payload_is_redacted_before_redis_write() -> None:
    redis = FakeRedis()
    settings = Settings()

    asyncio.run(
        enqueue_deferrable_request(
            redis=redis,
            settings=settings,
            request=_request(),
            tenant="tenant-a",
            feature="long_form_generation",
            request_id="req-queue-redaction",
        )
    )

    queued = DeferredRequest.from_json(redis.queue[0])
    content = str(queued.request.messages[0].content)
    metadata = str(queued.request.metadata)

    assert "alice@example.com" not in content
    assert "555-123-4567" not in content
    assert "123-45-6789" not in content
    assert "bob@example.com" not in metadata
    assert "[REDACTED_EMAIL]" in content
