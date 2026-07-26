from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: str = Field(..., examples=["user"])
    content: Any
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="allow")


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool | None = False
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionMessage(BaseModel):
    role: str = "assistant"
    content: Any = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionMessage
    finish_reason: str | None = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "chatcmpl_01HZY7Y9Q8M6V4D9N7G7Y2K9Q1",
                "object": "chat.completion",
                "created": 1784998800,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "The support ticket should be classified as billing.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 22, "completion_tokens": 10, "total_tokens": 32},
            }
        }
    )


class QueuedResponse(BaseModel):
    status: str
    idempotency_key: str
    queue: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "queued",
                "idempotency_key": "0f4c2f8de1a7d5fdc8bb5a50d717e7f47c477807c5b2ab2f26ecb9d4c7de7b11",
                "queue": "llm_gateway:queue:ready",
            }
        }
    )


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "missing_required_headers",
                    "message": "Missing required headers: x-tenant-id, x-feature, x-request-id",
                    "request_id": "req_01HZY7Y9Q8M6V4D9N7G7Y2K9Q1",
                }
            }
        }
    )


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "service": "LLM Gateway",
                "environment": "prod",
            }
        }
    )


class ReadyResponse(BaseModel):
    status: str
    service: str
    environment: str
    providers: list[str]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ready",
                "service": "LLM Gateway",
                "environment": "prod",
                "providers": ["openai", "anthropic"],
            }
        }
    )


class ProviderStatus(BaseModel):
    name: str
    circuit_state: str
    model: str | None = None
    configured: bool = True
    has_api_key: bool = False
    api_base: str | None = None
    request_classes: list[str] | None = None
    priority: int | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "openai",
                "circuit_state": "closed",
                "model": "gpt-4o-mini",
                "configured": True,
                "has_api_key": True,
                "api_base": "https://api.openai.com/v1",
                "request_classes": ["classification", "long_form_generation"],
                "priority": 1,
            }
        }
    )


class ProvidersResponse(BaseModel):
    providers: list[ProviderStatus]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "providers": [
                    {
                        "name": "openai",
                        "circuit_state": "closed",
                        "model": "gpt-4o-mini",
                        "configured": True,
                        "has_api_key": True,
                    },
                    {
                        "name": "anthropic",
                        "circuit_state": "open",
                        "model": "anthropic/claude-3-5-haiku-latest",
                        "configured": True,
                        "has_api_key": True,
                    },
                ]
            }
        }
    )


class RuntimeProviderRequest(BaseModel):
    name: str
    model: str
    api_key: str | None = None
    api_base: str | None = None
    request_classes: list[str] = Field(default_factory=lambda: ["classification"])
    priority: int = 100
    enabled: bool = True

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "my-openai-compatible-api",
                "model": "openai/my-model",
                "api_key": "sk-your-key",
                "api_base": "https://inference.example.com/v1",
                "request_classes": ["classification"],
                "priority": 1,
                "enabled": True,
            }
        }
    )


class RuntimeProviderPatch(BaseModel):
    model: str | None = None
    api_key: str | None = None
    api_base: str | None = None
    request_classes: list[str] | None = None
    priority: int | None = None
    enabled: bool | None = None


class RuntimeProviderResponse(BaseModel):
    name: str
    model: str | None = None
    api_key: str | None = None
    has_api_key: bool = False
    api_base: str | None = None
    request_classes: list[str] | None = None
    priority: int | None = None
    enabled: bool = True


class RuntimeProvidersResponse(BaseModel):
    providers: list[RuntimeProviderResponse]
