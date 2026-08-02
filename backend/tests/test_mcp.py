from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app.core.permissions import Scope
from app.core.security import create_access_token
from app.db.models import User
from app.discovery.search import SearchCandidate, SearchResult
from app.main import app
from app.mcp import tools as mcp_tools
from app.mcp.auth import TuxnewsTokenVerifier
from app.mcp.server import mcp, mcp_http_app, run_stdio
from fastapi.testclient import TestClient
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken


def test_mcp_health_is_available_without_session_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/mcp/health", headers={"Host": "testserver"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tuxnews-mcp"}


def test_sse_requires_a_read_capable_access_token() -> None:
    with TestClient(app) as client:
        response = client.get("/mcp/", headers={"Host": "testserver"})

    assert response.status_code == 401


def test_sse_transport_is_mounted_and_verifier_accepts_access_token() -> None:
    assert {getattr(route, "path", None) for route in mcp_http_app.routes} >= {"/", "/messages"}


@pytest.mark.asyncio
async def test_sse_verifier_accepts_an_existing_access_token(db_session, monkeypatch) -> None:
    user = User(email="mcp-user@example.com", password_hash="fixture-password")
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(user.id, scopes=[Scope.CONTENT_READ.value])

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr("app.mcp.auth.SessionFactory", session_context)
    verified = await TuxnewsTokenVerifier().verify_token(token)

    assert verified is not None
    assert verified.client_id == f"user:{user.id}"
    assert verified.scopes == [Scope.CONTENT_READ.value]


@pytest.mark.asyncio
async def test_shared_server_is_compatible_with_stdio_client() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools} == {
        "search_articles",
        "get_daily_briefing",
        "save_article_md",
        "add_rss_source",
        "rate_article",
    }
    assert {str(resource.uri) for resource in resources} == {
        "news://briefing/today",
        "news://archive/latest",
    }


def test_stdio_uses_the_shared_server(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(mcp, "run", fake_run)
    run_stdio()

    assert calls == [{"transport": "stdio", "show_banner": False}]


@pytest.mark.asyncio
async def test_mcp_tools_hide_auth_dependencies_and_advertise_mutation_confirmation() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    by_name = {tool.name: tool for tool in tools}
    assert "token" not in by_name["save_article_md"].inputSchema["properties"]
    assert "ctx" not in by_name["save_article_md"].inputSchema["properties"]
    assert by_name["save_article_md"].inputSchema["properties"]["confirm"]["default"] is False
    assert by_name["save_article_md"].annotations.readOnlyHint is False


@pytest.mark.asyncio
async def test_search_tool_returns_provenance_and_untrusted_content_warning(monkeypatch) -> None:
    async def fake_search(query: str, *, max_results: int):
        assert query == "linux"
        assert max_results == 3
        return SearchResult(
            query=query,
            provider="fixture",
            provider_version="v1",
            candidates=(
                SearchCandidate(
                    title="External title",
                    snippet="External snippet",
                    url="https://example.com/story",
                    published_at=None,
                    provider="fixture",
                    provider_version="v1",
                ),
            ),
        )

    monkeypatch.setattr(mcp_tools, "search_external_articles", fake_search)
    token = AccessToken(
        token="fixture",
        client_id="agent:1",
        scopes=["news:read"],
        claims={"sub": "1", "type": "agent"},
    )
    response = await mcp_tools.search_articles(
        "linux",
        max_results=3,
        token=token,
        ctx=SimpleNamespace(request_id="request-1"),
    )

    assert response.security_context == "UNTRUSTED_EXTERNAL_DATA"
    assert "untrusted data" in response.warning
    assert response.articles[0].provider == "fixture"


@pytest.mark.asyncio
async def test_mutating_tool_requires_explicit_confirmation() -> None:
    token = AccessToken(
        token="fixture",
        client_id="agent:1",
        scopes=["feedback:write"],
        claims={"sub": "1", "type": "agent"},
    )

    with pytest.raises(ToolError, match="confirmation"):
        await mcp_tools.rate_article_tool(
            1,
            "like",
            token=token,
            ctx=SimpleNamespace(request_id="request-2"),
        )
