import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_user_settings_are_versioned_isolated_and_safety_capped(auth_client: AsyncClient) -> None:
    first = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "settings-first@example.com", "password": "correct horse battery staple"},
    )
    second = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "settings-second@example.com", "password": "correct horse battery staple"},
    )
    first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}

    defaults = await auth_client.get("/api/v1/preferences/settings", headers=first_headers)
    assert defaults.status_code == 200
    assert defaults.json()["version"] == 1
    assert defaults.json()["llm_profile"] == "eco"
    assert defaults.json()["discovery_max_queries"] == 8

    updated = await auth_client.patch(
        "/api/v1/preferences/settings",
        headers=first_headers,
        json={
            "version": 1,
            "llm_profile": "cloud",
            "score_weights": {"semantic": 0.2, "reputation": 0.3, "feedback": 0.5},
            "discovery_max_queries": 2,
            "briefing_max_items": 4,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["llm_profile"] == "cloud"
    assert updated.json()["score_weights"] == {"semantic": 0.2, "reputation": 0.3, "feedback": 0.5}
    assert updated.json()["discovery_max_queries"] == 2

    other = await auth_client.get("/api/v1/preferences/settings", headers=second_headers)
    assert other.status_code == 200
    assert other.json()["version"] == 1
    assert other.json()["llm_profile"] == "eco"
    assert other.json()["discovery_max_queries"] == 8

    stale = await auth_client.patch(
        "/api/v1/preferences/settings",
        headers=first_headers,
        json={"version": 1, "llm_profile": "hybrid"},
    )
    assert stale.status_code == 409

    unsafe = await auth_client.patch(
        "/api/v1/preferences/settings",
        headers=first_headers,
        json={"version": 2, "discovery_max_queries": 9},
    )
    assert unsafe.status_code == 422
