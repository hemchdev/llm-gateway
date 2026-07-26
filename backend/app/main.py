from contextlib import asynccontextmanager
from time import perf_counter

import asyncio
import json
import logging

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .cache import SemanticCache
from .chaos import ChaosController, router as chaos_router
from .circuit_breaker import ProviderCircuitBreaker
from .config import Settings, get_settings
from .health import HealthTracker, router as health_router
from .limits import TenantLimiter, enforce_tenant_limits
from .logging_utils import configure_logging, log_request_received, reset_log_context, set_log_context
from .metrics import metrics_payload, record_request_metrics
from .models import (
    ChatCompletionResponse,
    ChatRequest,
    ErrorResponse,
    ProviderStatus,
    ProvidersResponse,
    QueuedResponse,
    RuntimeProviderPatch,
    RuntimeProviderRequest,
    RuntimeProviderResponse,
    RuntimeProvidersResponse,
)
from .providers import DeferredRequestQueued, complete_chat
from .queue import DeferredRequest, DeferredRequestWorker, get_redis
from .runtime_config import ProviderStore
from .security import require_admin_auth, require_chaos_admin_auth, require_metrics_auth

REQUIRED_HEADERS = ("x-tenant-id", "x-feature", "x-request-id")
HEADER_EXEMPT_PATHS = ("/health", "/ready", "/metrics", "/admin/chaos", "/docs", "/redoc", "/openapi.json")
HEADER_EXEMPT_PREFIXES = ("/admin/providers",)
API_TITLE = "LLM Gateway API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "A production-oriented OpenAI-compatible chat completions gateway that routes requests across configured LLM "
    "providers, tracks provider health in Redis, applies circuit breaking and hedging, enforces tenant limits, "
    "supports deferred retry queues, exposes Prometheus metrics, and provides controlled chaos testing."
)

logger = logging.getLogger("llm_gateway")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or "unknown"


def _error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
    )


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        413: "request_too_large",
        422: "invalid_request",
        429: "rate_limited",
        502: "provider_error",
        503: "service_unavailable",
        504: "provider_timeout",
    }.get(status_code, "internal_error")


def _public_message(status_code: int, detail: object) -> str:
    if status_code >= 500:
        return "Service temporarily unavailable" if status_code == 503 else "Internal server error"
    if isinstance(detail, str) and detail:
        return detail
    return "Request failed"


def _docs_html(title: str, openapi_schema: dict) -> HTMLResponse:
    schema = json.dumps(openapi_schema)
    return HTMLResponse(
        f"""
<!DOCTYPE html>
<html>
<head>
  <title>{title} - Swagger UI</title>
  <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function() {{
      SwaggerUIBundle({{
        spec: {schema},
        dom_id: "#swagger-ui",
        deepLinking: true,
        displayRequestDuration: true
      }});
    }};
  </script>
</body>
</html>
        """.strip()
    )


def _redoc_html(title: str, openapi_schema: dict) -> HTMLResponse:
    schema = json.dumps(openapi_schema)
    return HTMLResponse(
        f"""
<!DOCTYPE html>
<html>
<head>
  <title>{title} - ReDoc</title>
</head>
<body>
  <div id="redoc-container"></div>
  <script src="https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js"></script>
  <script>
    Redoc.init({schema}, {{}}, document.getElementById("redoc-container"));
  </script>
</body>
</html>
        """.strip()
    )


def _error_response_doc(status_code: int, description: str, code: str, message: str) -> dict:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message": message,
                        "request_id": "req_01HZY7Y9Q8M6V4D9N7G7Y2K9Q1",
                    }
                }
            }
        },
    }


def _is_header_exempt(path: str) -> bool:
    return path in HEADER_EXEMPT_PATHS or any(path.startswith(prefix) for prefix in HEADER_EXEMPT_PREFIXES)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_startup()
    configure_logging(settings.log_level)
    health_tracker = HealthTracker.from_settings(settings)
    breaker = ProviderCircuitBreaker(
        health_tracker=health_tracker,
        error_rate_threshold=settings.circuit_error_rate_threshold,
        p95_latency_threshold_seconds=settings.circuit_p95_latency_threshold_seconds,
        min_samples=settings.circuit_min_samples,
        reset_seconds=settings.circuit_reset_seconds,
        half_open_probe_rate=settings.circuit_half_open_probe_rate,
    )
    queue_redis = get_redis(settings)
    chaos_controller = ChaosController(queue_redis, settings.chaos_key_prefix)
    semantic_cache = SemanticCache.from_settings(queue_redis, settings)
    tenant_limiter = TenantLimiter(queue_redis, settings)

    async def process_deferred_request(item: DeferredRequest) -> None:
        await complete_chat(
            request=item.request,
            settings=settings,
            tenant=item.tenant,
            feature=item.feature,
            request_id=item.request_id,
            health_tracker=health_tracker,
            breaker=breaker,
            queue_redis=queue_redis,
            semantic_cache=semantic_cache,
            allow_queue=False,
            chaos_controller=chaos_controller,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker_task: asyncio.Task | None = None
        if settings.queue_worker_enabled:
            worker = DeferredRequestWorker(
                redis=queue_redis,
                settings=settings,
                process_request=process_deferred_request,
            )
            app.state.queue_worker = worker
            worker_task = asyncio.create_task(worker.run())
            app.state.queue_worker_task = worker_task
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()

    app = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Feature", "X-Request-Id", "X-Tenant-Id"],
    )
    app.state.health_tracker = health_tracker
    app.state.settings = settings
    app.state.provider_breaker = breaker
    app.state.queue_redis = queue_redis
    app.state.semantic_cache = semantic_cache
    app.state.tenant_limiter = tenant_limiter
    app.state.queue_worker_task = None
    app.state.chaos_controller = chaos_controller

    app.include_router(health_router)
    app.include_router(chaos_router, dependencies=[Depends(require_chaos_admin_auth)])

    async def authenticated_metrics(_: None = Depends(require_metrics_auth)) -> str:
        return metrics_payload()

    app.add_api_route(
        "/metrics",
        authenticated_metrics,
        methods=["GET"],
        response_model=str,
        response_class=PlainTextResponse,
        tags=["metrics"],
        summary="Scrape Prometheus metrics",
        description=(
            "Returns Prometheus text exposition metrics for gateway request volume, latency, errors, circuit state, "
            "failover events, queue depth, hedged calls, and estimated tenant cost."
        ),
        responses={
            200: {
                "description": "Prometheus text exposition format.",
                "content": {
                    "text/plain": {
                        "example": (
                            "# HELP requests_total Total gateway requests\n"
                            "# TYPE requests_total counter\n"
                            'requests_total{provider="openai",tenant="acme",feature="classification",status="success"} 42.0\n'
                        )
                    }
                },
            },
            401: _error_response_doc(401, "Missing metrics authentication.", "unauthorized", "Unauthorized"),
            403: _error_response_doc(403, "Invalid metrics authentication.", "forbidden", "Forbidden"),
            503: _error_response_doc(
                503,
                "Metrics authentication is not configured.",
                "service_unavailable",
                "Service temporarily unavailable",
            ),
        },
    )

    if settings.enable_docs:
        async def authenticated_openapi(_: None = Depends(require_admin_auth)) -> dict:
            return app.openapi()

        app.add_api_route(
            "/openapi.json",
            authenticated_openapi,
            methods=["GET"],
            include_in_schema=False,
        )

        @app.get("/docs", include_in_schema=False, dependencies=[Depends(require_admin_auth)])
        async def swagger_ui() -> HTMLResponse:
            return _docs_html(API_TITLE, app.openapi())

        @app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_admin_auth)])
        async def redoc_ui() -> HTMLResponse:
            return _redoc_html(API_TITLE, app.openapi())

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        log_level = logging.ERROR if exc.status_code >= 500 else logging.WARNING
        logger.log(
            log_level,
            "http_exception",
            extra={"llm_gateway_fields": {"status_code": exc.status_code, "detail": exc.detail}},
            exc_info=exc.status_code >= 500,
        )
        return _error_response(
            status_code=exc.status_code,
            code=_error_code(exc.status_code),
            message=_public_message(exc.status_code, exc.detail),
            request_id=_request_id(request),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning(
            "request_validation_failed",
            extra={"llm_gateway_fields": {"errors": exc.errors()}},
        )
        return _error_response(
            status_code=422,
            code="invalid_request",
            message="Invalid request",
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=True)
        return _error_response(
            status_code=500,
            code="internal_error",
            message="Internal server error",
            request_id=_request_id(request),
        )

    @app.middleware("http")
    async def require_gateway_headers(request: Request, call_next):
        tokens = set_log_context(
            tenant_id=request.headers.get("x-tenant-id", "unknown"),
            feature=request.headers.get("x-feature", "unknown"),
            request_id=request.headers.get("x-request-id", "unknown"),
        )
        try:
            start = perf_counter()
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > settings.max_request_body_bytes:
                return _error_response(
                    status_code=413,
                    code="request_too_large",
                    message="Request body too large",
                    request_id=_request_id(request),
                )

            if _is_header_exempt(request.url.path):
                return await call_next(request)

            missing_headers = [header for header in REQUIRED_HEADERS if not request.headers.get(header)]
            if missing_headers:
                record_request_metrics(
                    provider="none",
                    tenant=request.headers.get("x-tenant-id", "unknown"),
                    feature=request.headers.get("x-feature", "unknown"),
                    status="missing_headers",
                    latency_seconds=perf_counter() - start,
                )
                return _error_response(
                    status_code=400,
                    code="missing_required_headers",
                    message=f"Missing required headers: {', '.join(missing_headers)}",
                    request_id=_request_id(request),
                )
            return await call_next(request)
        finally:
            reset_log_context(tokens)

    @app.post(
        "/v1/chat/completions",
        response_model=ChatCompletionResponse,
        tags=["chat"],
        summary="Create a chat completion",
        description=(
            "Accepts an OpenAI-compatible chat completion request. Required metadata is supplied with "
            "X-Tenant-Id, X-Feature, and X-Request-Id headers. The gateway chooses an available provider, "
            "applies tenant controls, caching, circuit breaking, hedging, and then returns an OpenAI-shaped response."
        ),
        responses={
            200: {
                "description": "A completed chat response from the selected provider.",
                "content": {
                    "application/json": {
                        "example": ChatCompletionResponse.model_config["json_schema_extra"]["example"]
                    }
                },
            },
            202: {
                "model": QueuedResponse,
                "description": "The request was deferrable and all providers were unavailable, so it was queued.",
            },
            400: _error_response_doc(
                400,
                "Required metadata headers are missing.",
                "missing_required_headers",
                "Missing required headers: x-tenant-id, x-feature, x-request-id",
            ),
            401: _error_response_doc(401, "Provider authentication failed.", "unauthorized", "Unauthorized"),
            413: _error_response_doc(413, "The request body exceeds the configured limit.", "request_too_large", "Request body too large"),
            422: _error_response_doc(422, "The request body is not a valid chat completion request.", "invalid_request", "Invalid request"),
            429: _error_response_doc(429, "Tenant rate limit or provider rate limit was exceeded.", "rate_limited", "Rate limit exceeded"),
            502: _error_response_doc(502, "The selected provider returned an error.", "provider_error", "Provider error"),
            503: _error_response_doc(
                503,
                "No healthy provider is currently available.",
                "service_unavailable",
                "Service temporarily unavailable",
            ),
            504: _error_response_doc(504, "The selected provider timed out.", "provider_timeout", "Provider timeout"),
        },
    )
    async def chat_completions(
        http_request: Request,
        request: ChatRequest = Body(
            ...,
            examples=[
                {
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": "Classify support tickets into billing, technical, or sales."},
                        {"role": "user", "content": "I was charged twice for my subscription this month."},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 64,
                    "metadata": {"request_class": "classification", "difficulty": "easy"},
                }
            ],
            openapi_examples={
                "classification": {
                    "summary": "Classify a support ticket",
                    "description": "A latency-sensitive classification request using OpenAI-compatible chat messages.",
                    "value": {
                        "model": "gpt-4o-mini",
                        "messages": [
                            {
                                "role": "system",
                                "content": "Classify support tickets into billing, technical, or sales.",
                            },
                            {
                                "role": "user",
                                "content": "I was charged twice for my subscription this month.",
                            },
                        ],
                        "temperature": 0.2,
                        "max_tokens": 64,
                        "metadata": {"request_class": "classification", "difficulty": "easy"},
                    },
                }
            },
        ),
        runtime_settings: Settings = Depends(get_settings),
    ) -> ChatCompletionResponse:
        tenant = http_request.headers["x-tenant-id"]
        feature = http_request.headers["x-feature"]
        request_id = http_request.headers["x-request-id"]

        log_request_received(tenant, feature, request_id, request)
        await enforce_tenant_limits(http_request.app.state.tenant_limiter, tenant, feature, request)
        try:
            return await complete_chat(
                request=request,
                settings=runtime_settings,
                tenant=tenant,
                feature=feature,
                request_id=request_id,
                health_tracker=http_request.app.state.health_tracker,
                breaker=http_request.app.state.provider_breaker,
                queue_redis=http_request.app.state.queue_redis,
                semantic_cache=http_request.app.state.semantic_cache,
                chaos_controller=http_request.app.state.chaos_controller,
            )
        except DeferredRequestQueued as queued:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "queued",
                    "idempotency_key": queued.idempotency_key,
                    "queue": queued.queue_name,
                },
            )

    def runtime_provider_store() -> ProviderStore:
        runtime_settings: Settings = app.state.settings
        queue_redis = app.state.queue_redis
        if not ProviderStore.is_configured(runtime_settings):
            raise HTTPException(status_code=503, detail="Runtime provider storage is not configured")
        return ProviderStore.from_settings(queue_redis, runtime_settings)

    @app.get(
        "/admin/providers",
        response_model=RuntimeProvidersResponse,
        tags=["admin"],
        summary="List runtime providers",
        description="Lists custom OpenAI-compatible providers stored in Redis. API keys are masked in responses.",
        dependencies=[Depends(require_chaos_admin_auth)],
        responses={
            200: {"description": "Runtime providers stored in Redis."},
            403: _error_response_doc(403, "Missing or invalid X-Admin-Key.", "forbidden", "Forbidden"),
            503: _error_response_doc(503, "Runtime provider storage is not configured.", "service_unavailable", "Service temporarily unavailable"),
        },
    )
    async def list_runtime_providers() -> RuntimeProvidersResponse:
        store = runtime_provider_store()
        providers = [RuntimeProviderResponse.model_validate(provider) for provider in await store.list()]
        return RuntimeProvidersResponse(providers=providers)

    @app.post(
        "/admin/providers",
        response_model=RuntimeProviderResponse,
        tags=["admin"],
        summary="Create runtime provider",
        description=(
            "Creates or replaces a custom provider in Redis. Use an OpenAI-compatible model name such as "
            "openai/my-model and an api_base ending in /v1 for personal inference endpoints."
        ),
        dependencies=[Depends(require_chaos_admin_auth)],
        responses={
            200: {"description": "Provider saved with the API key encrypted at rest."},
            403: _error_response_doc(403, "Missing or invalid X-Admin-Key.", "forbidden", "Forbidden"),
            503: _error_response_doc(503, "Runtime provider storage is not configured.", "service_unavailable", "Service temporarily unavailable"),
        },
    )
    async def create_runtime_provider(provider: RuntimeProviderRequest) -> RuntimeProviderResponse:
        store = runtime_provider_store()
        saved = await store.create(provider.model_dump(exclude_none=True))
        return RuntimeProviderResponse.model_validate(saved)

    @app.put(
        "/admin/providers/{provider_name}",
        response_model=RuntimeProviderResponse,
        tags=["admin"],
        summary="Update runtime provider",
        description="Updates a custom Redis-backed provider. Omit api_key to keep the existing encrypted key.",
        dependencies=[Depends(require_chaos_admin_auth)],
        responses={
            200: {"description": "Provider updated."},
            403: _error_response_doc(403, "Missing or invalid X-Admin-Key.", "forbidden", "Forbidden"),
            404: _error_response_doc(404, "Provider was not found.", "not_found", "Provider not found"),
            503: _error_response_doc(503, "Runtime provider storage is not configured.", "service_unavailable", "Service temporarily unavailable"),
        },
    )
    async def update_runtime_provider(provider_name: str, patch: RuntimeProviderPatch) -> RuntimeProviderResponse:
        store = runtime_provider_store()
        updates = patch.model_dump(exclude_unset=True, exclude_none=True)
        updated = await store.update(provider_name, updates)
        if updated is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        return RuntimeProviderResponse.model_validate(updated)

    @app.delete(
        "/admin/providers/{provider_name}",
        response_model=dict[str, bool],
        tags=["admin"],
        summary="Delete runtime provider",
        description="Deletes a custom Redis-backed provider.",
        dependencies=[Depends(require_chaos_admin_auth)],
        responses={
            200: {"description": "Provider deletion result."},
            403: _error_response_doc(403, "Missing or invalid X-Admin-Key.", "forbidden", "Forbidden"),
            503: _error_response_doc(503, "Runtime provider storage is not configured.", "service_unavailable", "Service temporarily unavailable"),
        },
    )
    async def delete_runtime_provider(provider_name: str) -> dict[str, bool]:
        store = runtime_provider_store()
        return {"deleted": await store.delete(provider_name)}

    @app.get(
        "/v1/providers",
        response_model=ProvidersResponse,
        tags=["chat"],
        summary="List provider status",
        description=(
            "Lists configured providers and their current circuit breaker state. "
            "Runtime providers stored in Redis are included when provider storage is configured."
        ),
        responses={
            200: {
                "description": "Configured providers with current circuit breaker state.",
                "content": {
                    "application/json": {
                        "example": ProvidersResponse.model_config["json_schema_extra"]["example"]
                    }
                },
            },
            400: _error_response_doc(
                400,
                "Required metadata headers are missing.",
                "missing_required_headers",
                "Missing required headers: x-tenant-id, x-feature, x-request-id",
            ),
        },
    )
    async def list_providers(http_request: Request) -> ProvidersResponse:
        runtime_settings: Settings = http_request.app.state.settings
        breaker: ProviderCircuitBreaker = http_request.app.state.provider_breaker
        provider_statuses: dict[str, ProviderStatus] = {}
        tenant = http_request.headers["x-tenant-id"]
        feature = http_request.headers["x-feature"]

        configured_models = {
            "openai": runtime_settings.openai_model,
            "anthropic": runtime_settings.anthropic_model,
            "gemini": runtime_settings.gemini_model,
        }
        for provider in runtime_settings.configured_provider_names:
            decision = await breaker.allow_provider(provider, tenant, feature)
            provider_statuses[provider] = ProviderStatus(
                name=provider,
                circuit_state=decision.state.value,
                model=configured_models.get(provider),
                configured=True,
                has_api_key=bool(runtime_settings.provider_api_key(provider)),
                api_base=runtime_settings.provider_api_base(provider),
            )

        queue_redis = http_request.app.state.queue_redis
        if ProviderStore.is_configured(runtime_settings) and hasattr(queue_redis, "smembers"):
            store = ProviderStore.from_settings(queue_redis, runtime_settings)
            for provider in await store.list():
                provider_name = str(provider["name"])
                decision = await breaker.allow_provider(provider_name, tenant, feature)
                provider_statuses[provider_name] = ProviderStatus(
                    name=provider_name,
                    circuit_state=decision.state.value,
                    model=provider.get("model"),
                    configured=bool(provider.get("enabled", True)),
                    has_api_key=bool(provider.get("has_api_key")),
                    api_base=provider.get("api_base"),
                    request_classes=provider.get("request_classes"),
                    priority=provider.get("priority"),
                )

        return ProvidersResponse(providers=list(provider_statuses.values()))

    return app


app = create_app()
