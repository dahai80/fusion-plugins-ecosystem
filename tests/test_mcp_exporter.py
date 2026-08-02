"""MCPExporter 测试。"""

from __future__ import annotations


from fusion_plugins_ecosystem.mcp_exporter import MCPExporter
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)


def _make_manifest(
    plugin_id: str = "test_plugin",
    capabilities: list[PluginCapability] | None = None,
    has_mcp: bool = True,
) -> PluginManifest:
    caps = capabilities or []
    if has_mcp and PluginCapability.MCP_TOOL not in caps:
        caps.append(PluginCapability.MCP_TOOL)
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="测试插件",
        capabilities=caps,
        params=[
            PluginParam(
                name="text",
                type="string",
                description="输入文本",
                required=True,
            ),
            PluginParam(
                name="count",
                type="int",
                description="数量",
                required=False,
                default=10,
            ),
            PluginParam(
                name="mode",
                type="string",
                description="模式",
                enum=["fast", "slow"],
            ),
        ],
    )


def test_list_tools_no_plugins_returns_empty() -> None:
    registry = PluginRegistry()
    exporter = MCPExporter(registry)
    assert exporter.list_tools() == []


def test_list_tools_with_mcp_capability() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "mcp__plugin__p1"
    assert tool["description"] == "测试插件"
    assert "text" in tool["inputSchema"]["properties"]
    assert "count" in tool["inputSchema"]["properties"]
    assert "mode" in tool["inputSchema"]["properties"]
    assert "text" in tool["inputSchema"]["required"]
    assert "count" not in tool["inputSchema"]["required"]


def test_list_tools_filters_out_non_mcp() -> None:
    registry = PluginRegistry()
    # 无 MCP_TOOL 能力的插件
    registry.register(
        _make_manifest(
            "p1",
            capabilities=[PluginCapability.CLAUDE_SKILL],
            has_mcp=False,
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert len(tools) == 0


def test_manifest_to_mcp_tool_includes_enum_and_default() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    props = tools[0]["inputSchema"]["properties"]
    assert props["mode"]["enum"] == ["fast", "slow"]
    assert props["count"]["default"] == 10


async def test_call_tool_logs_and_returns_mcp_response() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    exporter = MCPExporter(registry)
    result = await exporter.call_tool("p1", {"text": "hello"})
    assert result["isError"] is False
    assert "content" in result
    assert "p1" in result["content"][0]["text"]


async def test_call_tool_propagates_arguments_in_response() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    exporter = MCPExporter(registry)
    args = {"text": "world", "count": 5}
    result = await exporter.call_tool("p1", args)
    text_content = result["content"][0]["text"]
    assert "world" in text_content


def test_gateway_info_returns_mcp_metadata() -> None:
    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    desk = DeskRuntime(mcp_gateway_port=9999)
    registry = PluginRegistry(desk=desk)
    exporter = MCPExporter(registry)
    info = exporter.gateway_info()
    assert info["port"] == 9999
    assert info["transport"] == "stdio"
    assert info["protocol_version"] == "2026-07-28"
    assert info["tools_count"] == 0  # 未注册插件


def test_list_tools_multiple_plugins() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p2"))
    registry.register(_make_manifest("p3"))
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert len(tools) == 3
    tool_ids = {t["name"] for t in tools}
    assert tool_ids == {"mcp__plugin__p1", "mcp__plugin__p2", "mcp__plugin__p3"}


def test_mcp_exporter_uses_registry_desk_by_default() -> None:
    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    exporter = MCPExporter(registry)
    assert exporter.desk is desk


def test_mcp_exporter_explicit_desk_overrides() -> None:
    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    desk1 = DeskRuntime()
    desk2 = DeskRuntime()
    registry = PluginRegistry(desk=desk1)
    exporter = MCPExporter(registry, desk=desk2)
    assert exporter.desk is desk2


# ── Edge cases ──


def test_list_tools_plugin_no_params() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginManifest(
            id="no_params",
            name="No Params",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="无参数插件",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=(),
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert len(tools) == 1
    assert tools[0]["inputSchema"]["properties"] == {}
    assert "required" not in tools[0]["inputSchema"]


def test_list_tools_all_params_required() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginManifest(
            id="all_req",
            name="All Required",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="全部必填",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=(
                PluginParam(name="a", type="string", description="A", required=True),
                PluginParam(name="b", type="int", description="B", required=True),
            ),
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert set(tools[0]["inputSchema"]["required"]) == {"a", "b"}


def test_list_tools_unknown_param_type_falls_back() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginManifest(
            id="weird_type",
            name="Weird",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="未知类型",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=(PluginParam(name="x", type="unknown_type", description="X"),),
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    assert tools[0]["inputSchema"]["properties"]["x"]["type"] == "string"


def test_list_tools_param_no_enum_no_default() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginManifest(
            id="bare_param",
            name="Bare",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="裸参数",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=(PluginParam(name="y", type="float", description="Y"),),
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    prop = tools[0]["inputSchema"]["properties"]["y"]
    assert "enum" not in prop
    assert "default" not in prop
    assert prop["type"] == "number"


async def test_call_tool_empty_arguments() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    exporter = MCPExporter(registry)
    result = await exporter.call_tool("p1", {})
    assert result["isError"] is False
    assert "content" in result


def test_gateway_info_with_registered_tools() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p2"))
    exporter = MCPExporter(registry)
    info = exporter.gateway_info()
    assert info["tools_count"] == 2


def test_list_tools_mixed_capabilities() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("mcp_only"))
    registry.register(
        _make_manifest(
            "skill_only",
            capabilities=[PluginCapability.CLAUDE_SKILL],
            has_mcp=False,
        )
    )
    registry.register(
        _make_manifest(
            "mcp_and_skill",
            capabilities=[PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL],
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    tool_ids = {t["name"] for t in tools}
    assert tool_ids == {"mcp__plugin__mcp_only", "mcp__plugin__mcp_and_skill"}
    assert "skill_only" not in tool_ids


def test_list_tools_param_types_mapping() -> None:
    from fusion_plugins_ecosystem.schema import PluginParamType

    registry = PluginRegistry()
    registry.register(
        PluginManifest(
            id="type_test",
            name="Type Test",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="类型映射测试",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=(
                PluginParam(name="s", type=PluginParamType.STRING, description="S"),
                PluginParam(name="i", type=PluginParamType.INT, description="I"),
                PluginParam(name="b", type=PluginParamType.BOOL, description="B"),
                PluginParam(name="f", type=PluginParamType.FLOAT, description="F"),
                PluginParam(name="a", type=PluginParamType.ARRAY, description="A"),
                PluginParam(name="o", type=PluginParamType.OBJECT, description="O"),
            ),
        )
    )
    exporter = MCPExporter(registry)
    tools = exporter.list_tools()
    props = tools[0]["inputSchema"]["properties"]
    assert props["s"]["type"] == "string"
    assert props["i"]["type"] == "integer"
    assert props["b"]["type"] == "boolean"
    assert props["f"]["type"] == "number"
    assert props["a"]["type"] == "array"
    assert props["o"]["type"] == "object"
