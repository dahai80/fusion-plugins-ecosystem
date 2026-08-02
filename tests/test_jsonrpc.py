"""MCP JSON-RPC 处理器测试。"""

from __future__ import annotations


from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import (
    MCPHandler,
    _error_response,
    _result_response,
)
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import PluginParamType


def _make_registry(*manifests: PluginManifest) -> PluginRegistry:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    for m in manifests:
        registry.register(m)
    return registry


def _mcp_tool_manifest() -> PluginManifest:
    return PluginManifest(
        id="test_tool",
        name="Test Tool",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="A test MCP tool",
        capabilities=(PluginCapability.MCP_TOOL,),
        params=(
            PluginParam(
                name="input",
                type=PluginParamType.STRING,
                description="input text",
                required=True,
            ),
            PluginParam(
                name="count",
                type=PluginParamType.INT,
                description="repeat count",
                required=False,
                default=1,
            ),
        ),
    )


def _no_tool_manifest() -> PluginManifest:
    return PluginManifest(
        id="no_tool",
        name="No Tool",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="Not an MCP tool",
        capabilities=(),
    )


# ── 响应构造 ──


def test_error_response_structure() -> None:
    resp = _error_response(1, -32600, "Invalid Request")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["error"]["code"] == -32600
    assert resp["error"]["message"] == "Invalid Request"


def test_error_response_with_data() -> None:
    resp = _error_response(2, -32603, "Internal error", data={"detail": "x"})
    assert resp["error"]["data"] == {"detail": "x"}


def test_result_response_structure() -> None:
    resp = _result_response(1, {"tools": []})
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"] == {"tools": []}


# ── initialize ──


async def test_initialize_negotiates_protocol() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._initialize(
        {
            "protocolVersion": "2026-07-28",
            "clientInfo": {"name": "test-client", "version": "1.0"},
        }
    )
    assert result["protocolVersion"] == "2026-07-28"
    assert result["serverInfo"]["name"] == "fusion-plugins-ecosystem"
    assert "tools" in result["capabilities"]


async def test_initialize_fallback_to_latest() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._initialize(
        {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "old-client"},
        }
    )
    assert result["protocolVersion"] == "2024-11-05"


async def test_initialize_unknown_version_uses_default() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._initialize(
        {
            "protocolVersion": "2099-01-01",
            "clientInfo": {"name": "future-client"},
        }
    )
    assert result["protocolVersion"] == "2026-07-28"


# ── ping ──


async def test_ping_returns_empty() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._ping({})
    assert result == {}


# ── tools/list ──


async def test_tools_list_returns_mcp_tools() -> None:
    registry = _make_registry(_mcp_tool_manifest(), _no_tool_manifest())
    handler = MCPHandler(registry=registry)
    result = await handler._tools_list({})
    assert len(result["tools"]) == 1
    tool = result["tools"][0]
    assert tool["name"] == "test_tool"
    assert tool["title"] == "Test Tool"
    assert "inputSchema" in tool
    assert "annotations" in tool


async def test_tools_list_empty_when_no_tools() -> None:
    registry = _make_registry(_no_tool_manifest())
    handler = MCPHandler(registry=registry)
    result = await handler._tools_list({})
    assert result["tools"] == []


async def test_tools_list_includes_annotations() -> None:
    registry = _make_registry(_mcp_tool_manifest())
    handler = MCPHandler(registry=registry)
    result = await handler._tools_list({})
    tool = result["tools"][0]
    assert "readOnlyHint" in tool["annotations"]
    assert "destructiveHint" in tool["annotations"]


# ── tools/call ──


async def test_tools_call_unknown_tool_returns_error() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._tools_call({"name": "nonexistent", "arguments": {}})
    assert result["isError"] is True


async def test_tools_call_disabled_plugin_returns_error() -> None:
    registry = _make_registry(_mcp_tool_manifest())
    handler = MCPHandler(registry=registry)
    result = await handler._tools_call(
        {"name": "test_tool", "arguments": {"input": "hi"}}
    )
    assert result["isError"] is True


# ── resources ──


async def test_resources_list_returns_empty() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._resources_list({})
    assert result["resources"] == []


async def test_resources_read_returns_not_implemented() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._resources_read({"uri": "test://resource"})
    assert len(result["contents"]) == 1


# ── prompts ──


async def test_prompts_list_returns_empty() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._prompts_list({})
    assert result["prompts"] == []


async def test_prompts_get_returns_not_implemented() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._prompts_get({"name": "test_prompt"})
    assert "messages" in result


# ── server/discover ──


async def test_server_discover_returns_info() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    result = await handler._server_discover({})
    assert result["name"] == "fusion-plugins-ecosystem"
    assert result["protocolVersion"] == "2026-07-28"
    assert result["capabilities"]["tools"] is True


# ── handle() dispatch ──


async def test_handle_dispatches_initialize() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "clientInfo": {"name": "test"},
            },
        }
    )
    assert resp["id"] == 1
    assert "result" in resp


async def test_handle_unknown_method_returns_error() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "nonexistent/method",
            "params": {},
        }
    )
    assert resp["error"]["code"] == -32601


async def test_handle_notification_returns_none() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {},
        }
    )
    assert resp is None


async def test_handle_ping_returns_result() -> None:
    registry = _make_registry()
    handler = MCPHandler(registry=registry)
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "ping",
        }
    )
    assert resp["result"] == {}
