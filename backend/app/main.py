from time import perf_counter

from fastapi import Depends, FastAPI, HTTPException

from .chaos import maybe_inject_failure
from .circuit_breaker import CircuitBreaker
from .config import Settings, get_settings
from .health import router as health_router
from .metrics import REQUEST_COUNT, REQUEST_LATENCY, metrics_response
from .models import ChatRequest, ChatResponse
from .providers import complete_chat


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    breaker = CircuitBreaker(
        failure_threshold=settings.circuit_failure_threshold,
        reset_seconds=settings.circuit_reset_seconds,
    )
    app.state.circuit_breaker = breaker

    app.include_router(health_router)
    app.add_api_route("/metrics", metrics_response, methods=["GET"], include_in_schema=False)

    @app.post("/v1/chat/completions", response_model=ChatResponse)
    async def chat_completions(
        request: ChatRequest,
        runtime_settings: Settings = Depends(get_settings),
    ) -> ChatResponse:
        route = "/v1/chat/completions"
        start = perf_counter()

        if not breaker.allow_request():
            REQUEST_COUNT.labels(route=route, status="circuit_open").inc()
            raise HTTPException(status_code=503, detail="Provider circuit is open")

        try:
            maybe_inject_failure(runtime_settings)
            response = await complete_chat(request, runtime_settings)
        except Exception:
            breaker.record_failure()
            REQUEST_COUNT.labels(route=route, status="error").inc()
            raise

        breaker.record_success()
        REQUEST_COUNT.labels(route=route, status="success").inc()
        REQUEST_LATENCY.labels(route=route).observe(perf_counter() - start)
        return response

    return app


app = create_app()
