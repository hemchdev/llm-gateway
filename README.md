# LLM Gateway

FastAPI scaffold for an LLM gateway with provider routing through LiteLLM, health checks, metrics, Redis queue helpers, circuit breaking, hedging primitives, and optional chaos injection.

## Local Development

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -c "import fastapi, litellm"
python -m pytest backend\tests
uvicorn app.main:app --app-dir backend --reload
```

## Docker

```powershell
docker compose up --build
```

The API listens on `http://localhost:8000`, Prometheus on `http://localhost:9090`, and Grafana on `http://localhost:3000`.
