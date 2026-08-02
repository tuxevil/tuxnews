import pytest
from app.audit.service import record_audit
from app.core.security import hash_password
from app.db.models import User
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_audit_export_is_admin_only_tenant_filtered_and_redacted(
    auth_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "audit-owner@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    user_id = registered.json()["user"]["id"]
    user_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    admin = User(
        email="audit-admin@example.com",
        password_hash=hash_password("admin-password-123"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.flush()
    record_audit(
        db_session,
        user_id=user_id,
        tenant_id=user_id,
        action="test.secret_event",
        resource_type="test",
        resource_id="1",
        outcome="success",
        details={"refresh_token": "do-not-export", "safe": "value"},
    )
    await db_session.commit()

    forbidden = await auth_client.get("/api/v1/admin/audit-events", headers=user_headers)
    assert forbidden.status_code == 403

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "admin-password-123"},
    )
    assert login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    exported = await auth_client.get(
        f"/api/v1/admin/audit-events?tenant_id={user_id}",
        headers=admin_headers,
    )
    assert exported.status_code == 200
    event = next(item for item in exported.json()["items"] if item["action"] == "test.secret_event")
    assert event["tenant_id"] == user_id
    assert event["details"] == {
        "refresh_token": "[REDACTED]",
        "safe": "value",
        "actor_type": "user",
        "actor_id": str(user_id),
        "tenant_id": user_id,
    }
