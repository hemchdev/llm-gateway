from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response


REQUESTS_TOTAL = Counter(
    "requests_total",
    "Total gateway requests",
    ["provider", "tenant", "feature", "status"],
)

REQUEST_LATENCY_SECONDS = Histogram(
    "request_latency_seconds",
    "Gateway request latency in seconds",
    ["provider", "tenant", "feature", "status"],
)

ERRORS_TOTAL = Counter(
    "errors_total",
    "Total gateway errors",
    ["provider", "tenant", "feature", "error_type"],
)

HEDGED_CALLS_TOTAL = Counter(
    "hedged_calls_total",
    "Total hedged provider calls fired after the primary delay",
    ["primary_provider", "hedge_provider", "tenant", "feature"],
)

CIRCUIT_STATE = Gauge(
    "circuit_state",
    "Provider circuit state: 0=closed, 1=half_open, 2=open",
    ["provider"],
)

FAILOVER_EVENTS_TOTAL = Counter(
    "failover_events_total",
    "Total provider failover events",
    ["from_provider", "to_provider", "tenant", "feature", "reason"],
)

QUEUE_DEPTH = Gauge(
    "queue_depth",
    "Deferred request queue depth",
    ["queue"],
)

COST_USD_TOTAL = Counter(
    "cost_usd_total",
    "Estimated gateway cost in USD",
    ["provider", "tenant", "feature"],
)


def record_request_metrics(
    provider: str,
    tenant: str,
    feature: str,
    status: str,
    latency_seconds: float,
) -> None:
    REQUESTS_TOTAL.labels(provider=provider, tenant=tenant, feature=feature, status=status).inc()
    REQUEST_LATENCY_SECONDS.labels(
        provider=provider,
        tenant=tenant,
        feature=feature,
        status=status,
    ).observe(latency_seconds)


def record_error_metrics(provider: str, tenant: str, feature: str, error_type: str) -> None:
    ERRORS_TOTAL.labels(provider=provider, tenant=tenant, feature=feature, error_type=error_type).inc()


def record_hedged_call_metrics(primary_provider: str, hedge_provider: str, tenant: str, feature: str) -> None:
    HEDGED_CALLS_TOTAL.labels(
        primary_provider=primary_provider,
        hedge_provider=hedge_provider,
        tenant=tenant,
        feature=feature,
    ).inc()


def record_circuit_state(provider: str, state: str) -> None:
    value = {"closed": 0, "half_open": 1, "open": 2}.get(state, 0)
    CIRCUIT_STATE.labels(provider=provider).set(value)


def record_failover_event(
    from_provider: str,
    to_provider: str,
    tenant: str,
    feature: str,
    reason: str,
) -> None:
    FAILOVER_EVENTS_TOTAL.labels(
        from_provider=from_provider,
        to_provider=to_provider,
        tenant=tenant,
        feature=feature,
        reason=reason,
    ).inc()


def set_queue_depth(queue: str, depth: int) -> None:
    QUEUE_DEPTH.labels(queue=queue).set(depth)


def record_cost(provider: str, tenant: str, feature: str, usd: float) -> None:
    COST_USD_TOTAL.labels(provider=provider, tenant=tenant, feature=feature).inc(usd)


def metrics_payload() -> str:
    return generate_latest().decode("utf-8")


def metrics_response() -> Response:
    return Response(metrics_payload(), media_type="text/plain; version=0.0.4")
