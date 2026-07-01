from dataclasses import dataclass
from enum import Enum
from time import monotonic


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int
    reset_seconds: float
    failures: int = 0
    opened_at: float | None = None
    state: CircuitState = CircuitState.CLOSED

    def allow_request(self) -> bool:
        if self.state != CircuitState.OPEN:
            return True
        if self.opened_at is None:
            return False
        if monotonic() - self.opened_at >= self.reset_seconds:
            self.state = CircuitState.HALF_OPEN
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = monotonic()
