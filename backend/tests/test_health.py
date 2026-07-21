from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint() -> None:
    response = TestClient(app).get(
        "/health",
        headers={
            "X-Tenant-Id": "test-tenant",
            "X-Feature": "health",
            "X-Request-Id": "test-request",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
