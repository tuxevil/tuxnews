import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_sets_http_only_refresh_cookie_and_rejects_bad_login(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "reader@example.com", "password": "correct horse battery staple"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "reader@example.com"
    assert "password" not in response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1/auth" in cookie

    invalid_login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "reader@example.com", "password": "not the password"},
    )
    assert invalid_login.status_code == 401


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_the_entire_token_family(auth_client: AsyncClient) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "rotation@example.com", "password": "correct horse battery staple"},
    )
    original_refresh = auth_client.cookies.get("tuxnews_refresh")
    assert registered.status_code == 201
    assert original_refresh

    rotated = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    replacement_refresh = auth_client.cookies.get("tuxnews_refresh")
    assert rotated.status_code == 200
    assert replacement_refresh and replacement_refresh != original_refresh

    reused = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh})
    assert reused.status_code == 401

    family_revoked = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": replacement_refresh}
    )
    assert family_revoked.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_cookie(auth_client: AsyncClient) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "logout@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201

    logged_out = await auth_client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 200
    assert 'tuxnews_refresh=""' in logged_out.headers["set-cookie"]
    assert "Max-Age=0" in logged_out.headers["set-cookie"]

    refreshed = await auth_client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 401
