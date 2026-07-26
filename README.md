# LLM Gateway

A production-style, OpenAI-compatible LLM gateway for routing chat completion requests across multiple providers with Redis-backed health tracking, circuit breaking, failover, semantic caching, deferred retries, tenant controls, and full Prometheus/Grafana observability.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-F46800?logo=grafana&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-000000?logo=nextdotjs&logoColor=white)
![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)

## Demo Recording

Add the 60-90 second GitHub demo recording here:

```md
https://github.com/user-attachments/assets/your-recording-link
```

Recommended recording flow:

1. Show the frontend console at `http://localhost:3001`.
2. Show a saved provider in Redis.
3. Send a successful chat request.
4. Open Grafana and show healthy traffic.
5. Inject chaos into a provider.
6. Send traffic again and show the breaker opening or traffic rerouting.
7. Return to Grafana and show the error spike, circuit state, and failover panels.

## What This Project Shows

LLM Gateway is designed as a demo of reliability patterns around LLM APIs:

- OpenAI-compatible `/v1/chat/completions` endpoint.
- Custom OpenAI-compatible provider registration from the frontend.
- Redis-backed provider storage with encrypted API keys.
- Provider health tracking with sliding windows.
- Circuit breaker states: `closed`, `open`, and `half_open`.
- Automatic provider failover.
- Hedged requests for latency-sensitive workloads.
- Redis-backed retry queue for deferrable requests.
- Semantic response cache.
- Per-tenant rate limits and monthly budgets.
- Structured request logging with PII redaction.
- Prometheus metrics and Grafana dashboard.
- Chaos endpoint for demoing failures.

## Architecture

```text
Frontend Console
  |
  | /api/gateway/* proxy
  v
FastAPI Backend
  |
  | LiteLLM
  v
OpenAI-compatible Providers

FastAPI Backend
  |
  +--> Redis: providers, health windows, cache, queue, tenant limits
  +--> Prometheus: /metrics scrape
  +--> Grafana: provisioned dashboard
```

Important files:

- `frontend/`: Next.js console for provider setup, chat, chaos, and monitoring links.
- `backend/app/main.py`: FastAPI app, chat endpoint, provider admin API, health, readiness, metrics.
- `backend/app/providers.py`: LiteLLM routing, provider calls, failover, queue handoff.
- `backend/app/runtime_config.py`: Redis-backed provider store with Fernet-encrypted API keys.
- `backend/app/health.py`: Redis sliding-window health tracker.
- `backend/app/circuit_breaker.py`: provider circuit breaker.
- `backend/app/queue.py`: Redis retry queue with idempotency keys.
- `backend/app/chaos.py`: synthetic error/latency injection.
- `monitoring/`: Prometheus and Grafana provisioning.

## Quick Start

From the project root:

```powershell
docker-compose up --build -d
```

Check containers:

```powershell
docker-compose ps
```

You should see:

```text
backend
frontend
redis
prometheus
grafana
```

Open:

| Service | URL |
| --- | --- |
| Frontend console | `http://localhost:3001` |
| Backend API | `http://localhost:8000` |
| API docs | `http://localhost:8000/docs` |
| Grafana | `http://localhost:3000` |
| Prometheus | `http://localhost:9090` |

Grafana default login:

```text
admin / admin
```

## Using The Frontend Console

Open:

```text
http://localhost:3001
```

Enter the admin key:

```text
dev-admin-token
```

If you changed `.env`, use your value for:

```text
LLM_GATEWAY_ADMIN_API_KEY
```

### Add A Custom OpenAI-Compatible API

For a provider such as Logfare, OpenRouter, a local inference server, or any API that supports OpenAI-style chat completions:

```text
Provider name: logfare-free
Model: openai/deepseek-v4-flash
API base URL: https://logfare.ai/v1
API key: your-api-key
Request classes: classification
Priority: 1
Enabled: checked
```

Then click:

```text
Save provider
```

The API key is encrypted before being stored in Redis. The frontend only displays a masked key.

### Send A Chat Request

Use:

```text
Tenant: demo-tenant
Feature: classification
Request class: classification
Model: deepseek-v4-flash
Message: Hello, explain FastAPI in one sentence.
```

Click:

```text
Send chat
```

The request goes through the gateway, not directly from the browser to the provider.

## Personal Inference Servers

If your OpenAI-compatible server runs on the internet:

```text
API base URL: https://your-server.com/v1
Model: openai/your-model
```

If your server runs on your own computer and the backend runs in Docker, do not use `localhost` for the provider URL. Inside Docker, `localhost` means the backend container.

Use:

```text
http://host.docker.internal:PORT/v1
```

Example:

```text
API base URL: http://host.docker.internal:11434/v1
Model: openai/your-local-model
API key: local
```

## API Usage

Every chat request must include:

```text
X-Tenant-Id
X-Feature
X-Request-Id
```

Example:

```powershell
$body = @'
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "user", "content": "Say hello in one sentence."}
  ],
  "metadata": {
    "request_class": "classification"
  }
}
'@

curl.exe -X POST http://localhost:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "X-Tenant-Id: demo-tenant" `
  -H "X-Feature: classification" `
  -H "X-Request-Id: demo-1" `
  -d $body
```

## Admin APIs

Admin endpoints require:

```text
X-Admin-Key: dev-admin-token
```

List saved providers:

```powershell
curl.exe http://localhost:8000/admin/providers `
  -H "X-Admin-Key: dev-admin-token"
```

Create a provider:

```powershell
$provider = @'
{
  "name": "logfare-free",
  "model": "openai/deepseek-v4-flash",
  "api_base": "https://logfare.ai/v1",
  "api_key": "your-api-key",
  "request_classes": ["classification"],
  "priority": 1,
  "enabled": true
}
'@

curl.exe -X POST http://localhost:8000/admin/providers `
  -H "Content-Type: application/json" `
  -H "X-Admin-Key: dev-admin-token" `
  -d $provider
```

Inject chaos:

```powershell
$chaos = @'
{
  "provider": "logfare-free",
  "duration_seconds": 60,
  "rate": 1.0,
  "error_type": "server_error",
  "latency_ms": 0
}
'@

curl.exe -X POST http://localhost:8000/admin/chaos `
  -H "Content-Type: application/json" `
  -H "X-Admin-Key: dev-admin-token" `
  -d $chaos
```

## Recording The GitHub Demo

Use this simple script while recording:

```text
This is my LLM Gateway.
It lets me save OpenAI-compatible providers with an API URL and key.
The key is encrypted in Redis.
Now I send a chat request through the gateway.
Prometheus collects metrics, and Grafana shows traffic, errors, latency, circuit state, queue depth, and cost.
Now I inject chaos into a provider.
The gateway records failures and opens the circuit breaker.
The dashboard shows the error spike and provider state change.
```

Suggested recording steps:

1. Start the stack with `docker-compose up --build -d`.
2. Open `http://localhost:3001`.
3. Refresh the provider list.
4. Save or show a custom provider.
5. Send a successful chat request.
6. Open `http://localhost:3000/d/llm-gateway/llm-gateway`.
7. Click `Chaos` for the provider in the frontend.
8. Send chat again.
9. Show Grafana panels updating.
10. Stop recording and add the video link under **Demo Recording**.

## Observability

Prometheus scrapes:

```text
http://backend:8000/metrics
```

Grafana dashboard panels:

- RPS by provider
- Error rate
- p95 latency
- Circuit state timeline
- Failover events
- Queue depth
- Cost per hour by tenant/feature

Useful Prometheus queries:

```promql
sum(rate(requests_total[1m])) by (provider)
sum(rate(errors_total[1m])) by (provider)
histogram_quantile(0.95, sum(rate(request_latency_seconds_bucket[5m])) by (le, provider))
circuit_state
failover_events_total
queue_depth
sum(rate(cost_usd_total[1h]) * 3600) by (tenant, feature)
```

## Configuration

Copy `.env.example` to `.env` and fill real secrets locally. Never commit `.env`.

Important settings:

```dotenv
LLM_GATEWAY_ADMIN_API_KEY=dev-admin-token
LLM_GATEWAY_METRICS_API_KEY=dev-metrics-token
ENCRYPTION_KEY=generate-a-fernet-key
LLM_GATEWAY_REDIS_URL=redis://redis:6379/0
LLM_GATEWAY_CIRCUIT_P95_LATENCY_THRESHOLD_SECONDS=15
```

Generate a Fernet key:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not commit API keys. If a key is accidentally pasted into chat, logs, or GitHub, rotate it.

## Troubleshooting

### `503 Service Unavailable`

Usually means no provider is currently usable.

Check:

1. Provider is saved.
2. Provider is enabled.
3. Request class matches the provider request class.
4. API base URL ends with `/v1`.
5. API key is correct.
6. Circuit breaker is not open.

Open the frontend and click:

```text
Refresh
```

Then look at `Routing status`.

### Saved Providers Disappear

Do not run:

```powershell
docker-compose down -v
```

The `-v` flag deletes Docker volumes, including Redis data.

Normal restart is safe:

```powershell
docker-compose restart
```

### Local API Does Not Work

If your provider runs on your laptop, use:

```text
http://host.docker.internal:PORT/v1
```

Do not use:

```text
http://localhost:PORT/v1
```

from inside Docker.

### Admin Calls Return `403`

Use the correct admin key:

```text
X-Admin-Key: dev-admin-token
```

or the value from `.env`.

## Tests

Run:

```powershell
.\venv\Scripts\Activate.ps1
python -m pytest backend\tests
```

The test suite covers:

- Health tracker sliding-window math.
- Circuit breaker state cycles.
- Deferrable retry queue and idempotency.
- Semantic cache.
- Tenant rate limits and budgets.
- PII redaction.
- Runtime provider encryption and routing.
- Production hardening behavior.

Pytest cache is stored in:

```text
.tmp/pytest-cache
```

## Notes For Reviewers

This project is intended as a portfolio/demo implementation of LLM gateway reliability patterns. It is not a managed production service. Before real production use, add CI/CD, secret management, persistent storage backups, stricter auth, rate-limit tuning, and deployment-specific hardening.

## License

MIT License. See `LICENSE`.
