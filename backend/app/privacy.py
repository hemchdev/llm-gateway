import copy
import hashlib
import json
import re
from typing import Any

from .models import ChatMessage, ChatRequest

EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact_text(value: str) -> str:
    redacted = SSN_PATTERN.sub("[REDACTED_SSN]", value)
    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
    redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def redacted_request(request: ChatRequest) -> ChatRequest:
    return ChatRequest.model_validate(redact_value(request.model_dump(mode="json")))


def normalize_prompt(messages: list[ChatMessage]) -> str:
    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        content = redact_value(message.content)
        if isinstance(content, str):
            content = " ".join(content.lower().split())
        normalized_messages.append({"role": message.role, "content": content})
    return json.dumps(normalized_messages, sort_keys=True, separators=(",", ":"))


def prompt_hash(messages: list[ChatMessage]) -> str:
    normalized = normalize_prompt(messages)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(redact_value(payload))
