from app.main import app
from fastapi.testclient import TestClient


def test_liveness() -> None:
    response = TestClient(app).get("/health/live", headers={"Host": "testserver"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert response.headers["X-Request-ID"]


def test_openapi_is_available() -> None:
    response = TestClient(app).get("/openapi.json", headers={"Host": "testserver"})
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Tuxnews"


def test_untrusted_host_is_rejected() -> None:
    response = TestClient(app).get("/health/live", headers={"Host": "evil.example"})
    assert response.status_code == 400
