import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.permissions import AgentScope
from app.main import app
from app.mcp import tools as mcp_tools
from app.mcp.schemas import (
    ArchiveResponse,
    DailyBriefingResponse,
    RatingResponse,
    SearchArticlesResponse,
    SourceResponse,
)
from app.mcp.server import mcp, mcp_http_app
from fastapi.testclient import TestClient
from fastmcp.client import Client
from fastmcp.client.transports import PythonStdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken

TOOL_NAMES = {
    "search_articles",
    "get_daily_briefing",
    "save_article_md",
    "add_rss_source",
    "rate_article",
}
RESOURCE_URIS = {"news://briefing/today", "news://archive/latest"}
BACKEND_ROOT = Path(__file__).parents[1]


def _agent_token(*scopes: str) -> AccessToken:
    return AccessToken(
        token="contract-fixture",
        client_id="agent:contract",
        scopes=list(scopes),
        claims={"sub": "1", "type": "agent"},
    )


@pytest.mark.asyncio
async def test_in_memory_catalog_is_the_v1_contract() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools} == TOOL_NAMES
    assert {str(resource.uri) for resource in resources} == RESOURCE_URIS


@pytest.mark.asyncio
async def test_stdio_subprocess_catalog_matches_in_memory_contract() -> None:
    transport = PythonStdioTransport(
        script_path=BACKEND_ROOT / "app" / "mcp" / "__main__.py",
        cwd=str(BACKEND_ROOT),
        env={"PYTHONPATH": str(BACKEND_ROOT)},
        python_cmd=sys.executable,
        keep_alive=False,
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools} == TOOL_NAMES
    assert {str(resource.uri) for resource in resources} == RESOURCE_URIS


def test_sse_route_contract_preserves_auth_and_streaming_paths() -> None:
    assert {getattr(route, "path", None) for route in mcp_http_app.routes} >= {"/", "/messages"}
    with TestClient(app) as client:
        health = client.get("/mcp/health", headers={"Host": "testserver"})
        unauthorized = client.get("/mcp/", headers={"Host": "testserver"})

    assert health.status_code == 200
    assert unauthorized.status_code == 401


@pytest.mark.asyncio
async def test_tool_schemas_have_output_contracts_and_bounded_inputs() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    by_name = {tool.name: tool for tool in tools}
    for name in TOOL_NAMES:
        assert getattr(by_name[name], "outputSchema", None), name
    assert by_name["search_articles"].inputSchema["properties"]["max_results"]["maximum"] == 20
    for name in {"save_article_md", "add_rss_source", "rate_article"}:
        assert by_name[name].inputSchema["properties"]["confirm"]["default"] is False


def test_all_content_models_advertise_untrusted_data() -> None:
    responses = (
        SearchArticlesResponse(query="fixture", articles=[], errors=[]),
        DailyBriefingResponse(found=False),
        ArchiveResponse(found=False),
        SourceResponse(id=1, name="fixture", url="https://example.com", tags=[], created=True),
        RatingResponse(feedback_id=1, article_id=1, rating="like", is_current=True),
    )
    assert {response.security_context for response in responses} == {"UNTRUSTED_EXTERNAL_DATA"}
    assert all("untrusted data" in response.warning for response in responses)


@pytest.mark.asyncio
async def test_scope_contract_rejects_a_tool_without_required_scope() -> None:
    with pytest.raises(ToolError, match="news:read"):
        await mcp_tools.search_articles(
            "linux",
            token=_agent_token(AgentScope.FEEDBACK_WRITE.value),
            ctx=SimpleNamespace(request_id="contract-request"),
        )
