import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

from .models import ChatRequest
from .privacy import redacted_request

logger = logging.getLogger("llm_gateway.requests")

_request_id = contextvars.ContextVar("request_id", default="unknown")
_tenant_id = contextvars.ContextVar("tenant_id", default="unknown")
_feature = contextvars.ContextVar("feature", default="unknown")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", _request_id.get()),
            "tenant_id": getattr(record, "tenant_id", _tenant_id.get()),
            "feature": getattr(record, "feature", _feature.get()),
        }
        extra_fields = getattr(record, "llm_gateway_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level.upper())

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def set_log_context(tenant_id: str, feature: str, request_id: str) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    return (
        _tenant_id.set(tenant_id or "unknown"),
        _feature.set(feature or "unknown"),
        _request_id.set(request_id or "unknown"),
    )


def reset_log_context(tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
    tenant_token, feature_token, request_token = tokens
    _tenant_id.reset(tenant_token)
    _feature.reset(feature_token)
    _request_id.reset(request_token)


def log_request_received(tenant: str, feature: str, request_id: str, request: ChatRequest) -> None:
    safe_request = redacted_request(request)
    payload: dict[str, Any] = {
        "event": "request_received",
        "tenant_id": tenant,
        "feature": feature,
        "request_id": request_id,
        "model": safe_request.model,
        "messages": [message.model_dump(mode="json", exclude_none=True) for message in safe_request.messages],
        "metadata": safe_request.metadata,
    }
    logger.info("request_received", extra={"llm_gateway_fields": payload})
