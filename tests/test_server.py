import sys
from dataclasses import replace

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from starlette.testclient import TestClient

from officespace_mcp.config import Settings
from officespace_mcp.server import create_server, http_app


async def test_real_stdio_protocol_and_offline_schema_discovery():
    async with Client(
        StdioServerParameters(command=sys.executable, args=["-m", "officespace_mcp.server"])
    ) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 11
        response = await client.call_tool("inspect_schema", {"search": "bookRoom"})
        assert not response.is_error
        assert response.structured_content["operations"][0]["name"] == "bookRoom"
        resource = await client.read_resource("officespace://schema")
        assert "type Query" in resource.contents[0].text


async def test_workflow_writes_fail_without_network_when_disabled(office):
    settings = replace(office.settings, enable_mutations=False)
    async with Client(create_server(settings, office.api.transport)) as client:
        response = await client.call_tool(
            "manage_employees",
            {"action": "deactivate", "changes": [{"person": "alice@example.com"}]},
        )
        assert response.is_error
        assert "disabled" in response.structured_content["error"]
    assert not office.calls


async def test_annotations_and_input_constraints():
    async with Client(create_server(Settings())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["find_people"].annotations.read_only_hint
        assert not tools["book_spaces"].annotations.read_only_hint
        assert (await client.call_tool("book_spaces", {"bookings": []})).is_error


def test_http_requires_a_separate_token():
    with pytest.raises(ValueError, match="MCP_BEARER_TOKEN"):
        http_app(create_server(Settings()), Settings())


def test_authenticated_http_mcp_handshake_and_host_boundary():
    settings = Settings(mcp_token="local-mcp-secret")
    app = http_app(create_server(settings), settings)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/mcp", json=message).status_code == 401
        headers = {"Authorization": "Bearer wrong", "Accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json=message, headers=headers).status_code == 401
        headers["Authorization"] = "Bearer local-mcp-secret"
        response = client.post("/mcp", json=message, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "OfficeSpace"
        headers["host"] = "attacker.example"
        assert client.post("/mcp", json=message, headers=headers).status_code >= 400
