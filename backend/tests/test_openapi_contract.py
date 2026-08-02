import json
from pathlib import Path

from app.main import app
from fastapi.testclient import TestClient

SNAPSHOT = Path(__file__).parents[1] / "openapi.json"
PRIVATE_PATH_PREFIX = "/api/v1/"
PUBLIC_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/invitations/accept",
    "/api/v1/auth/password-recovery",
    "/api/v1/auth/password-recovery/confirm",
    "/api/v1/auth/email-change/confirm",
}


def test_openapi_snapshot_is_current() -> None:
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert app.openapi() == expected


def test_openapi_contract_covers_public_surface_and_security() -> None:
    document = TestClient(app).get("/openapi.json", headers={"Host": "testserver"}).json()
    paths = document["paths"]
    required_paths = {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/me",
        "/api/v1/feed",
        "/api/v1/feedback",
        "/api/v1/preferences",
        "/api/v1/sources",
        "/api/v1/clusters",
        "/api/v1/briefings",
        "/api/v1/briefings/schedule",
    }
    assert required_paths <= paths.keys()
    assert document["info"]["version"] == "1.1.0"
    for path, operations in paths.items():
        if not path.startswith(PRIVATE_PATH_PREFIX):
            continue
        for operation in operations.values():
            if not isinstance(operation, dict) or path in PUBLIC_PATHS:
                continue
            if path == "/api/v1/health/live":
                continue
            assert operation.get("security"), f"missing auth contract for {path}"


def test_openapi_documents_scopes_and_common_error_responses() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    bearer = document["components"]["securitySchemes"]["HTTPBearer"]
    assert "content:read" in bearer["scopes"]
    assert "sources:write" in bearer["scopes"]
    protected = next(
        operation
        for path, operations in document["paths"].items()
        if path == "/api/v1/feed"
        for operation in operations.values()
        if isinstance(operation, dict)
    )
    for code in ("401", "403", "429", "503"):
        assert code in protected["responses"]
    public = document["paths"]["/api/v1/auth/login"]
    login_operation = public["post"]
    assert "401" not in login_operation["responses"]


def test_mutation_schemas_reject_unknown_fields() -> None:
    schemas = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["components"]["schemas"]
    for name in (
        "RegisterRequest",
        "LoginRequest",
        "RefreshRequest",
        "FeedbackCreate",
        "BriefingGenerateRequest",
        "BriefingScheduleUpdate",
    ):
        assert schemas[name]["additionalProperties"] is False
