import asyncio
import json

from app.health import HealthTracker


class FakeRedis:
    def __init__(self, events: list[tuple[float, dict]]) -> None:
        self.events = [(score, json.dumps(event)) for score, event in events]

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        added = 0
        for event, score in mapping.items():
            added += int(all(existing != event for _, existing in self.events))
            self.events.append((score, event))
        return added

    async def zrangebyscore(self, key: str, min_score: float, max_score: float) -> list[str]:
        return [event for score, event in self.events if min_score <= score <= max_score]

    async def zremrangebyscore(self, key: str, min_score: str | float, max_score: str | float) -> int:
        exclusive = isinstance(max_score, str) and max_score.startswith("(")
        threshold = float(str(max_score).lstrip("("))
        before = len(self.events)
        if exclusive:
            self.events = [(score, event) for score, event in self.events if score >= threshold]
        else:
            self.events = [(score, event) for score, event in self.events if score > threshold]
        return before - len(self.events)


def test_sliding_window_math_filters_old_events_and_calculates_percentiles() -> None:
    events = [
        (100.0, {"status": "success", "latency_seconds": 0.10, "timestamp": 100.0}),
        (101.0, {"status": "error", "latency_seconds": 0.20, "error_type": "timeout", "timestamp": 101.0}),
        (102.0, {"status": "success", "latency_seconds": 0.30, "timestamp": 102.0}),
        (103.0, {"status": "error", "latency_seconds": 0.40, "error_type": "rate_limit", "timestamp": 103.0}),
        (1.0, {"status": "error", "latency_seconds": 9.99, "error_type": "auth_failure", "timestamp": 1.0}),
    ]
    tracker = HealthTracker(redis=FakeRedis(events), window_seconds=5)

    summary = asyncio.run(
        tracker.summary(provider="mock", tenant="tenant-a", feature="chat", now=105.0)
    )

    assert summary.total == 4
    assert summary.success == 2
    assert summary.success_rate == 0.5
    assert summary.p50_latency_seconds == 0.25
    assert summary.p95_latency_seconds == 0.385
    assert summary.p99_latency_seconds == 0.397
    assert summary.errors["timeout"] == 1
    assert summary.errors["rate_limit"] == 1
    assert summary.errors["auth_failure"] == 0
    assert summary.errors["server_error"] == 0
    assert summary.errors["content_filter"] == 0


def test_health_tracker_records_success_and_error_events_in_timestamp_sets() -> None:
    redis = FakeRedis([])
    tracker = HealthTracker(redis=redis, window_seconds=60)

    asyncio.run(
        tracker.record_success(
            provider="mock",
            tenant="tenant-a",
            feature="classification",
            latency_seconds=0.10,
            request_id="req-success",
        )
    )
    asyncio.run(
        tracker.record_error(
            provider="mock",
            tenant="tenant-a",
            feature="classification",
            latency_seconds=0.30,
            request_id="req-error",
            error_type="auth_failure",
        )
    )

    assert len(redis.events) == 2
    stored_events = [json.loads(payload) for _, payload in redis.events]
    assert {event["status"] for event in stored_events} == {"success", "error"}
    assert any(event["error_type"] == "auth_failure" for event in stored_events)


def test_health_tracker_returns_empty_summary_for_empty_window() -> None:
    tracker = HealthTracker(redis=FakeRedis([]), window_seconds=60)

    summary = asyncio.run(tracker.summary(provider="mock", tenant="tenant-a", feature="classification", now=10.0))

    assert summary.total == 0
    assert summary.success == 0
    assert summary.success_rate == 0.0
    assert summary.p50_latency_seconds is None
    assert summary.p95_latency_seconds is None
    assert summary.p99_latency_seconds is None
