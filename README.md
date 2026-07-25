# Self-Healing LLM Gateway

A self healing, OpenAI compatible gateway for routing chat completions across multiple LLM providers, with health aware failover, hedged requests, deferrable retries, and full observability.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-F46800?logo=grafana&logoColor=white)
![Podman](https://img.shields.io/badge/Podman-892CA0?logo=podman&logoColor=white)
![Tests: pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)


## Demo

90 second screen recording placeholder:
Healthy traffic, then a provider degrading, the breaker tripping, traffic rerouting, and the breaker closing on recovery.

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Getting Started](#getting-started)
5. [Run Locally](#run-locally)
6. [Run With Podman](#run-with-podman)
7. [API Docs](#api-docs)
8. [Chaos Demo](#chaos-demo)
9. [Configuration](#configuration)
10. [Tests](#tests)
11. [Contributing](#contributing)
12. [License](#license)

## Overview

LLM Gateway is a FastAPI service that exposes an OpenAI compatible `POST /v1/chat/completions` endpoint while routing requests across multiple LLM providers through LiteLLM (https://github.com/BerriAI/litellm). It tracks per provider health, trips circuit breakers on degradation, hedges latency sensitive requests, queues deferrable work for retry, and injects chaos on demand, all backed by a Podman friendly Prometheus and Grafana observability stack.

## Features

1. OpenAI compatible API: a drop in `/v1/chat/completions` endpoint. Existing OpenAI SDK clients work with just a `base_url` change.
2. Multi provider routing: normalized across providers via LiteLLM, selected per request class.
3. Health tracking: Redis sliding window success rate, p50, p95, and p99 latency, and an error taxonomy per provider.
4. Circuit breaking: closed, open, and half open states with automatic recovery probing.
5. Hedged requests: backup requests fired after a configurable delay for latency sensitive classes.
6. Deferrable retry queue: Redis backed backoff and jitter with idempotency keys for degraded provider scenarios.
7. Chaos injection: an admin endpoint to simulate provider failure on demand for live failover demos.
8. Semantic cache: SHA 256 hashed, normalized prompt cache hits in Redis.
9. Model tiering and budgets: cheap model routing for easy requests, plus per tenant rate limits and monthly budgets.
10. PII redaction: requests and queued payloads are redacted before being logged or written.
11. Full observability: Prometheus metrics plus a provisioned Grafana dashboard out of the box.

## Architecture

1. `backend/app/main.py`: mounts the API, health check, metrics endpoint, and admin chaos route.
2. `backend/app/providers.py`: selects providers by request class, applies chaos, records metrics, handles failover, queues deferrable work.
3. `backend/app/health.py`: stores provider outcomes in Redis sorted sets, keyed by timestamp, for sliding window health summaries.
4. `backend/app/circuit_breaker.py`: tracks per provider closed, open, and half open state from HealthTracker error rate and p95 latency thresholds.
5. `backend/app/hedging.py`: fires backup requests for latency sensitive classes after a configurable delay.
6. `backend/app/queue.py`: Redis backed deferrable retries with exponential backoff, jitter, and idempotency keys derived from `X-Request-Id`.

Prometheus scrapes `/metrics`. Grafana provisions the LLM Gateway dashboard automatically.

## Getting Started

### Run Locally

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m pytest backend\tests
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 --workers 2
```

Every chat request must include:

```text
X-Tenant-Id: tenant-a
X-Feature: classification
X-Request-Id: req-123
```

### Run With Podman

Start the Podman VM if needed:

```powershell
podman machine start
```

Bring up the full stack:

```powershell
podman compose up --build -d
```

If your Podman install delegates Compose to the Docker Compose shim, this equivalent command also works:

```powershell
docker-compose up --build -d
```

Services once running:

1. API at `http://localhost:8000`
2. Swagger UI at `http://localhost:8000/docs`
3. ReDoc at `http://localhost:8000/redoc`
4. Prometheus at `http://localhost:9090`
5. Grafana at `http://localhost:3000`
6. Grafana dashboard at `http://localhost:3000/d/llm-gateway/llm-gateway`

Check health:

```powershell
podman ps
curl.exe http://localhost:8000/health
curl.exe http://localhost:8000/ready
curl.exe http://localhost:9090/-/healthy
curl.exe http://localhost:3000/api/health
```

### API Docs

Docs are enabled with `ENABLE_DOCS=true` and protected by the admin token. In local compose the default token is `dev-admin-token`, so either use Basic auth in a browser:

```text
http://admin:dev-admin-token@localhost:8000/docs
```

or fetch them with a header:

```powershell
curl.exe -H "Authorization: Bearer dev-admin-token" http://localhost:8000/docs
curl.exe -H "Authorization: Bearer dev-admin-token" http://localhost:8000/openapi.json
```

Disable docs in public deployments with:

```powershell
$env:ENABLE_DOCS = "false"
podman compose up --build -d
```

## Chaos Demo

The Compose stack calls real providers through LiteLLM. Put at least one real provider key in `.env` before sending chat traffic:

```dotenv
LLM_GATEWAY_OPENAI_API_KEY=sk-...
# or
LLM_GATEWAY_ANTHROPIC_API_KEY=sk-ant-...
# or
LLM_GATEWAY_GEMINI_API_KEY=...
```

1. Send healthy traffic:

```powershell
$body = @'
{"model":"gpt-4o-mini","messages":[{"role":"user","content":"classify this"}],"metadata":{"request_class":"classification"}}
'@

curl.exe -X POST http://localhost:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "X-Tenant-Id: tenant-demo" `
  -H "X-Feature: classification" `
  -H "X-Request-Id: healthy-1" `
  -d $body
```

2. Degrade the primary provider:

```powershell
$chaos = @'
{"provider":"openai","duration_seconds":120,"rate":1.0,"error_type":"server_error","latency_ms":25}
'@

curl.exe -X POST http://localhost:8000/admin/chaos `
  -H "Authorization: Bearer dev-admin-token" `
  -H "Content-Type: application/json" `
  -d $chaos
```

3. Send several more classification requests. The first few should fail against `openai`. Once the sliding health window trips the breaker, traffic reroutes to the next configured provider that has a key.

4. Watch it happen in Prometheus or Grafana:

```text
errors_total{provider="openai",error_type="server_error"}
circuit_state{provider="openai"}
failover_events_total{from_provider="openai"}
sum(rate(requests_total[1m])) by (provider)
histogram_quantile(0.95, sum(rate(request_latency_seconds_bucket[5m])) by (le, provider))
queue_depth
sum(rate(cost_usd_total[1h]) * 3600) by (tenant, feature)
```

After the chaos duration and circuit reset period pass, half open probes are allowed back to the recovering provider. A successful probe closes the breaker.

## Configuration

Use `.env` for real settings. It is gitignored. See `.env.example` for provider keys, request class routing, circuit breaker thresholds, hedging, queue retry, semantic cache, tenant limits, budgets, and cost estimation settings.

For a personal inference server that exposes an OpenAI compatible `/v1/chat/completions` API, route it through the OpenAI provider slot:

```dotenv
LLM_GATEWAY_PROVIDER_PREFERENCE=openai
LLM_GATEWAY_CLASSIFICATION_PROVIDER_PREFERENCE=openai
LLM_GATEWAY_LONG_FORM_GENERATION_PROVIDER_PREFERENCE=openai
LLM_GATEWAY_OPENAI_API_KEY=your-local-or-server-key
LLM_GATEWAY_OPENAI_API_BASE=http://localhost:11434/v1
LLM_GATEWAY_OPENAI_MODEL=your-model-name
LLM_GATEWAY_DEFAULT_MODEL=your-model-name
LLM_GATEWAY_CHEAP_MODEL=your-smaller-model-name
```

If your server does not require a real key, use the placeholder value it expects, such as `LLM_GATEWAY_OPENAI_API_KEY=local`.

Default request classes:

1. `classification`: interactive and latency sensitive.
2. `long_form_generation`: deferrable.

Resilience and cost controls:

1. Semantic cache stores successful responses in Redis using a SHA 256 hash of the normalized, redacted prompt.
2. Model tiering sends requests classified as easy to `LLM_GATEWAY_CHEAP_MODEL`.
3. Tenant rate limits are enforced per tenant and feature over a Redis TTL window.
4. Monthly budgets are enforced per tenant and feature using estimated prompt plus completion token cost.
5. Request logs and queued payloads are redacted before they are written.

## Tests

```powershell
.\venv\Scripts\Activate.ps1
python -m pytest backend\tests
```

Pytest cache is pinned to `.tmp/pytest-cache`.

Current coverage includes:

1. Circuit breaker closed, open, half open, closed cycles.
2. p95 latency and error rate trips.
3. Half open probe throttling and failed probe reopening.
4. Redis sliding window health math and taxonomy.
5. Idempotent queue enqueue, retry backoff and jitter, completed item dedupe, and max attempt failure.
6. Semantic cache prompt normalization and cache hits.
7. Model tiering for easy requests.
8. Tenant rate limits and monthly budget enforcement.
9. PII redaction before request logging and queue writes.

## Contributing

This started as a solo learning and portfolio project, but issues and pull requests are welcome if you spot a bug or want to extend it. Please open an issue describing the change before submitting a large pull request.

## License

Licensed under the MIT License. Add a `LICENSE` file with the MIT text if one is not in the repo yet, or replace this section with whichever license you prefer.
