from prometheus_client import Counter, Histogram, generate_latest
from starlette.responses import Response


REQUEST_COUNT = Counter(
    "llm_gateway_requests_total",
    "Total LLM gateway requests",
    ["route", "status"],
)

REQUEST_LATENCY = Histogram(
    "llm_gateway_request_latency_seconds",
    "LLM gateway request latency",
    ["route"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")
