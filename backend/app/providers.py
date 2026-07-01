from typing import Any

import litellm

from .config import Settings
from .models import ChatRequest, ChatResponse


async def complete_chat(request: ChatRequest, settings: Settings) -> ChatResponse:
    model = request.model or settings.default_model
    payload: dict[str, Any] = {
        "model": model,
        "messages": [message.model_dump() for message in request.messages],
        "timeout": settings.request_timeout_seconds,
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature

    response = await litellm.acompletion(**payload)
    choice = response.choices[0]
    content = choice.message.content or ""

    return ChatResponse(
        model=model,
        content=content,
        provider_response=response.model_dump() if hasattr(response, "model_dump") else {},
    )
