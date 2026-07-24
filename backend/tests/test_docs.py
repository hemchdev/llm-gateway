from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(enable_docs: bool = True) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                environment="test",
                enable_docs=enable_docs,
                admin_api_key="docs-admin-token",
                metrics_api_key="docs-metrics-token",
                allowed_origins="https://app.example.test",
            )
        )
    )


def test_docs_are_admin_authenticated_and_include_real_metadata() -> None:
    client = _client()

    assert client.get("/docs").status_code == 401
    docs_response = client.get("/docs", headers={"Authorization": "Bearer docs-admin-token"})
    assert docs_response.status_code == 200
    assert "LLM Gateway API" in docs_response.text
    assert "Create a chat completion" in docs_response.text

    openapi_response = client.get("/openapi.json", headers={"Authorization": "Bearer docs-admin-token"})
    assert openapi_response.status_code == 200
    schema = openapi_response.json()

    assert schema["info"]["title"] == "LLM Gateway API"
    assert schema["info"]["version"] == "1.0.0"
    assert "OpenAI-compatible chat completions gateway" in schema["info"]["description"]

    operations = {
        route: operation
        for route, methods in schema["paths"].items()
        for operation in methods.values()
    }
    assert operations["/v1/chat/completions"]["tags"] == ["chat"]
    assert operations["/admin/chaos"]["tags"] == ["admin"]
    assert operations["/health"]["tags"] == ["health"]
    assert operations["/ready"]["tags"] == ["health"]
    assert operations["/metrics"]["tags"] == ["metrics"]

    chat_operation = operations["/v1/chat/completions"]
    assert chat_operation["summary"] == "Create a chat completion"
    assert "examples" in chat_operation["requestBody"]["content"]["application/json"]
    assert "400" in chat_operation["responses"]
    assert "202" in chat_operation["responses"]
    assert "QueuedResponse" in str(chat_operation["responses"]["202"])

    chaos_operation = operations["/admin/chaos"]
    assert "403" in chaos_operation["responses"]
    assert "examples" in chaos_operation["requestBody"]["content"]["application/json"]

    metrics_operation = operations["/metrics"]
    assert metrics_operation["responses"]["200"]["content"]["text/plain"]["example"].startswith("# HELP")


def test_docs_can_be_disabled() -> None:
    client = _client(enable_docs=False)

    assert client.get("/docs", headers={"Authorization": "Bearer docs-admin-token"}).status_code == 404
    assert client.get("/redoc", headers={"Authorization": "Bearer docs-admin-token"}).status_code == 404
    assert client.get("/openapi.json", headers={"Authorization": "Bearer docs-admin-token"}).status_code == 404
