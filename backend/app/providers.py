import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import litellm
from fastapi import HTTPException

from .cache import SemanticCache
from .chaos import ChaosController, SyntheticProviderError
from .circuit_breaker import ProviderCircuitBreaker
from .config import Settings
from .health import HealthTracker
from .hedging import hedge
from .metrics import (
    record_cost,
    record_error_metrics,
    record_failover_event,
    record_hedged_call_metrics,
    record_request_metrics,
)
from .models import (
    ChatCompletionResponse,
    ChatRequest,
)
from .queue import enqueue_deferrable_request
from .tiering import tiered_model

logger = logging.getLogger("llm_gateway.providers")


@dataclass(frozen=True)
class ProviderRoute:
    name: str
    model: str
    api_key: str | None
    api_base: str | None = None


class DeferredRequestQueued(Exception):
    def __init__(self, idempotency_key: str, queue_name: str) -> None:
        self.idempotency_key = idempotency_key
        self.queue_name = queue_name
        super().__init__("Request queued for deferred processing")


def _provider_routes(settings: Settings, requested_model: str) -> dict[str, ProviderRoute]:
    return {
        "openai": ProviderRoute(
            name="openai",
            model=requested_model or settings.openai_model,
            api_key=settings.provider_api_key("openai"),
            api_base=settings.provider_api_base("openai"),
        ),
        "anthropic": ProviderRoute(
            name="anthropic",
            model=settings.anthropic_model,
            api_key=settings.provider_api_key("anthropic"),
        ),
        "gemini": ProviderRoute(
            name="gemini",
            model=settings.gemini_model,
            api_key=settings.provider_api_key("gemini"),
        ),
    }


def _available_routes(settings: Settings, request: ChatRequest, request_class: str) -> list[ProviderRoute]:
    selected_model = tiered_model(request, request_class, settings)
    routes = _provider_routes(settings, selected_model)
    candidates: list[ProviderRoute] = []
    for provider_name in settings.provider_preferences_for(request_class):
        route = routes.get(provider_name)
        if route is None:
            continue
        if route.api_key:
            candidates.append(route)
    return candidates


async def _allowed_routes(
    settings: Settings,
    request: ChatRequest,
    request_class: str,
    tenant: str,
    breaker: ProviderCircuitBreaker,
) -> list[ProviderRoute]:
    candidates = _available_routes(settings, request, request_class)
    allowed: list[ProviderRoute] = []
    for route in candidates:
        decision = await breaker.allow_provider(route.name, tenant, request_class)
        if decision.allowed:
            allowed.append(route)
    if candidates and allowed and candidates[0].name != allowed[0].name:
        skipped = candidates[0]
        record_failover_event(
            from_provider=skipped.name,
            to_provider=allowed[0].name,
            tenant=tenant,
            feature=request_class,
            reason=breaker.state_for(skipped.name).value,
        )
    return allowed


def _litellm_payload(request: ChatRequest, route: ProviderRoute, settings: Settings) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": route.model,
        "messages": [message.model_dump(exclude_none=True) for message in request.messages],
        "timeout": settings.request_timeout_seconds,
    }
    if route.api_key:
        payload["api_key"] = route.api_key
    if route.api_base:
        payload["api_base"] = route.api_base
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.stop is not None:
        payload["stop"] = request.stop
    if request.presence_penalty is not None:
        payload["presence_penalty"] = request.presence_penalty
    if request.frequency_penalty is not None:
        payload["frequency_penalty"] = request.frequency_penalty
    if request.tools is not None:
        payload["tools"] = request.tools
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice

    return payload


async def execute_provider(
    request: ChatRequest,
    route: ProviderRoute,
    settings: Settings,
) -> ChatCompletionResponse:
    provider_response = await litellm.acompletion(**_litellm_payload(request, route, settings))
    payload = provider_response.model_dump() if hasattr(provider_response, "model_dump") else dict(provider_response)
    payload["model"] = payload.get("model") or route.model
    return ChatCompletionResponse.model_validate(payload)


def classify_error(error: Exception) -> str:
    if isinstance(error, SyntheticProviderError):
        return error.error_type

    status_code = getattr(error, "status_code", None)
    if status_code in (401, 403):
        return "auth_failure"
    if status_code in (408, 504):
        return "timeout"
    if status_code == 429:
        return "rate_limit"
    if isinstance(status_code, int) and status_code >= 500:
        return "server_error"

    error_name = error.__class__.__name__.lower()
    message = str(error).lower()
    text = f"{error_name} {message}"

    if ("rate" in text and "limit" in text) or "429" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "content_filter" in text or "content filter" in text or "safety" in text:
        return "content_filter"
    if "auth" in text or "api key" in text or "unauthorized" in text or "forbidden" in text:
        return "auth_failure"
    return "server_error"


async def _record_health_success(
    health_tracker: HealthTracker,
    provider: str,
    tenant: str,
    feature: str,
    request_id: str,
    latency_seconds: float,
) -> None:
    try:
        await health_tracker.record_success(
            provider=provider,
            tenant=tenant,
            feature=feature,
            request_id=request_id,
            latency_seconds=latency_seconds,
        )
    except Exception:
        pass


async def _record_health_error(
    health_tracker: HealthTracker,
    provider: str,
    tenant: str,
    feature: str,
    request_id: str,
    latency_seconds: float,
    error_type: str,
) -> None:
    try:
        await health_tracker.record_error(
            provider=provider,
            tenant=tenant,
            feature=feature,
            request_id=request_id,
            latency_seconds=latency_seconds,
            error_type=error_type,
        )
    except Exception:
        pass


def _http_status_for_error(error_type: str) -> int:
    return {
        "rate_limit": 429,
        "timeout": 504,
        "server_error": 502,
        "content_filter": 400,
        "auth_failure": 401,
    }.get(error_type, 502)


async def _call_and_record(
    request: ChatRequest,
    route: ProviderRoute,
    settings: Settings,
    tenant: str,
    feature: str,
    request_id: str,
    health_tracker: HealthTracker,
    breaker: ProviderCircuitBreaker,
    chaos_controller: ChaosController | None,
) -> ChatCompletionResponse:
    started_at = perf_counter()
    try:
        if chaos_controller is not None:
            await chaos_controller.apply(route.name)
        response = await execute_provider(request, route, settings)
    except Exception as error:
        latency_seconds = perf_counter() - started_at
        error_type = classify_error(error)
        logger.error(
            "provider_call_failed",
            extra={
                "llm_gateway_fields": {
                    "provider": route.name,
                    "error_type": error_type,
                    "latency_seconds": latency_seconds,
                }
            },
            exc_info=True,
        )
        await _record_health_error(
            health_tracker=health_tracker,
            provider=route.name,
            tenant=tenant,
            feature=feature,
            request_id=request_id,
            latency_seconds=latency_seconds,
            error_type=error_type,
        )
        record_request_metrics(route.name, tenant, feature, "error", latency_seconds)
        record_error_metrics(route.name, tenant, feature, error_type)
        breaker.record_failure(route.name, error_type)
        if isinstance(error, SyntheticProviderError):
            raise HTTPException(status_code=_http_status_for_error(error_type), detail=error_type) from error
        raise

    latency_seconds = perf_counter() - started_at
    await _record_health_success(
        health_tracker=health_tracker,
        provider=route.name,
        tenant=tenant,
        feature=feature,
        request_id=request_id,
        latency_seconds=latency_seconds,
    )
    record_request_metrics(route.name, tenant, feature, "success", latency_seconds)
    estimated_cost = (response.usage.total_tokens / 1000) * settings.estimated_cost_per_1k_tokens
    record_cost(route.name, tenant, feature, estimated_cost)
    breaker.record_success(route.name)
    return response


def _request_class_for(request: ChatRequest, feature: str) -> str:
    return str((request.metadata or {}).get("request_class") or feature)


async def complete_chat(
    request: ChatRequest,
    settings: Settings,
    tenant: str,
    feature: str,
    request_id: str,
    health_tracker: HealthTracker,
    breaker: ProviderCircuitBreaker,
    queue_redis: Any | None = None,
    semantic_cache: SemanticCache | None = None,
    allow_queue: bool = True,
    chaos_controller: ChaosController | None = None,
) -> ChatCompletionResponse:
    request_class = _request_class_for(request, feature)
    if settings.semantic_cache_enabled and semantic_cache is not None:
        cached_response = await semantic_cache.get(request)
        if cached_response is not None:
            record_request_metrics("cache", tenant, request_class, "cache_hit", 0.0)
            return cached_response

    routes = await _allowed_routes(
        settings=settings,
        request=request,
        request_class=request_class,
        tenant=tenant,
        breaker=breaker,
    )
    if not routes:
        if (
            allow_queue
            and queue_redis is not None
            and settings.request_delivery_mode(request_class) == "deferrable"
        ):
            deferred = await enqueue_deferrable_request(
                redis=queue_redis,
                settings=settings,
                request=request,
                tenant=tenant,
                feature=request_class,
                request_id=request_id,
            )
            record_request_metrics("none", tenant, request_class, "queued", 0.0)
            raise DeferredRequestQueued(
                idempotency_key=deferred.idempotency_key,
                queue_name=settings.queue_name,
            )

        record_request_metrics("none", tenant, request_class, "circuit_open", 0.0)
        record_error_metrics("none", tenant, request_class, "server_error")
        raise HTTPException(status_code=503, detail="No healthy provider is available")

    primary = routes[0]

    async def primary_call() -> ChatCompletionResponse:
        return await _call_and_record(
            request=request,
            route=primary,
            settings=settings,
            tenant=tenant,
            feature=request_class,
            request_id=request_id,
            health_tracker=health_tracker,
            breaker=breaker,
            chaos_controller=chaos_controller,
        )

    if request_class not in settings.latency_sensitive_classes or len(routes) == 1:
        response = await primary_call()
        if settings.semantic_cache_enabled and semantic_cache is not None:
            await semantic_cache.set(request, response)
        return response

    fallback = routes[1]

    async def fallback_call() -> ChatCompletionResponse:
        record_hedged_call_metrics(primary.name, fallback.name, tenant, request_class)
        return await _call_and_record(
            request=request,
            route=fallback,
            settings=settings,
            tenant=tenant,
            feature=request_class,
            request_id=request_id,
            health_tracker=health_tracker,
            breaker=breaker,
            chaos_controller=chaos_controller,
        )

    response = await hedge(
        primary=primary_call,
        fallback=fallback_call,
        delay=settings.hedge_delay_seconds,
    )
    if settings.semantic_cache_enabled and semantic_cache is not None:
        await semantic_cache.set(request, response)
    return response
