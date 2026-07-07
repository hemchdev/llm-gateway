import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from time import monotonic

from .health import HealthTracker, ProviderHealth
from .metrics import record_circuit_state

logger = logging.getLogger("llm_gateway.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ProviderCircuitState:
    state: CircuitState = CircuitState.CLOSED
    opened_at: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CircuitDecision:
    provider: str
    allowed: bool
    state: CircuitState
    reason: str | None = None


class ProviderCircuitBreaker:
    def __init__(
        self,
        health_tracker: HealthTracker,
        error_rate_threshold: float,
        p95_latency_threshold_seconds: float,
        min_samples: int,
        reset_seconds: float,
        half_open_probe_rate: float,
        clock: Callable[[], float] = monotonic,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.health_tracker = health_tracker
        self.error_rate_threshold = error_rate_threshold
        self.p95_latency_threshold_seconds = p95_latency_threshold_seconds
        self.min_samples = min_samples
        self.reset_seconds = reset_seconds
        self.half_open_probe_rate = half_open_probe_rate
        self.clock = clock
        self.random_fn = random_fn
        self._states: dict[str, ProviderCircuitState] = {}

    async def allow_provider(self, provider: str, tenant: str, feature: str) -> CircuitDecision:
        state = self._state_for(provider)

        if state.state == CircuitState.OPEN and self._open_elapsed(state):
            state.state = CircuitState.HALF_OPEN
            state.reason = "probe"
            record_circuit_state(provider, state.state.value)

        if state.state == CircuitState.OPEN:
            return CircuitDecision(provider=provider, allowed=False, state=state.state, reason=state.reason)

        if state.state == CircuitState.HALF_OPEN:
            allowed = self.random_fn() < self.half_open_probe_rate
            return CircuitDecision(
                provider=provider,
                allowed=allowed,
                state=state.state,
                reason="half_open_probe" if allowed else "half_open_throttled",
            )

        health = await self.health_tracker.summary(provider=provider, tenant=tenant, feature=feature)
        unhealthy_reason = self._unhealthy_reason(health)
        if unhealthy_reason:
            self._open(provider, unhealthy_reason)
            logger.warning(
                "provider_circuit_opened",
                extra={
                    "llm_gateway_fields": {
                        "provider": provider,
                        "reason": unhealthy_reason,
                        "tenant_id": tenant,
                        "feature": feature,
                        "total": health.total,
                        "success_rate": health.success_rate,
                        "p95_latency_seconds": health.p95_latency_seconds,
                    }
                },
            )
            return CircuitDecision(provider=provider, allowed=False, state=CircuitState.OPEN, reason=unhealthy_reason)

        record_circuit_state(provider, CircuitState.CLOSED.value)
        return CircuitDecision(provider=provider, allowed=True, state=CircuitState.CLOSED)

    def record_success(self, provider: str) -> None:
        state = self._state_for(provider)
        if state.state == CircuitState.HALF_OPEN:
            state.state = CircuitState.CLOSED
            state.opened_at = None
            state.reason = None
            record_circuit_state(provider, state.state.value)

    def record_failure(self, provider: str, reason: str) -> None:
        state = self._state_for(provider)
        if state.state == CircuitState.HALF_OPEN:
            self._open(provider, reason)
            logger.warning(
                "provider_half_open_probe_failed",
                extra={"llm_gateway_fields": {"provider": provider, "reason": reason}},
            )

    def state_for(self, provider: str) -> CircuitState:
        return self._state_for(provider).state

    def _state_for(self, provider: str) -> ProviderCircuitState:
        return self._states.setdefault(provider, ProviderCircuitState())

    def _open_elapsed(self, state: ProviderCircuitState) -> bool:
        if state.opened_at is None:
            return False
        return self.clock() - state.opened_at >= self.reset_seconds

    def _open(self, provider: str, reason: str) -> None:
        state = self._state_for(provider)
        state.state = CircuitState.OPEN
        state.opened_at = self.clock()
        state.reason = reason
        record_circuit_state(provider, state.state.value)

    def _unhealthy_reason(self, health: ProviderHealth) -> str | None:
        if health.total < self.min_samples:
            return None

        error_rate = 1.0 - health.success_rate
        if error_rate >= self.error_rate_threshold:
            return "error_rate"

        p95 = health.p95_latency_seconds
        if p95 is not None and p95 >= self.p95_latency_threshold_seconds:
            return "p95_latency"

        return None
