"""插件 → MCP Tools 暴露。

对应 PRD「fusion-cowork 暴露 MCP Server，plugin-ecosystem 里所有插件能力
会自动注册为 MCP Tools 供给 Claude 调用」。

本模块负责将 PluginManifest 转换为 MCP Tool 描述，并通过 fusion-cowork
的 MCP 网关对外暴露。真实 MCP 协议封装由 fusion-cowork runtime 提供，
本模块只做描述生成 + 调用转发。
"""

from __future__ import annotations

import logging
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import (
    MCP_PROTOCOL_VERSION,
    _PARAM_TYPE_MAP,
)

logger = logging.getLogger(__name__)


# MCP Tool 描述模板（基于 MCP 规范的 tools/list 响应项）
_MCP_TOOL_TEMPLATE = {
    "jsonrpc": "2.0",
    "protocolVersion": MCP_PROTOCOL_VERSION,
}


class MCPExporter:
    """插件 → MCP Tools 暴露器。

    用法：
        exporter = MCPExporter(registry, desk)
        tools = exporter.list_tools()           # MCP tools/list 响应
        result = await exporter.call_tool(...)  # MCP tools/call 转发
    """

    def __init__(
        self,
        registry: PluginRegistry,
        desk: DeskRuntime | None = None,
    ) -> None:
        self.registry = registry
        self.desk = desk or registry.desk

    def list_tools(self) -> list[dict[str, Any]]:
        """生成 MCP tools/list 响应项列表。

        只暴露具备 MCP_TOOL 能力的插件。
        """
        tools: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            if PluginCapability.MCP_TOOL not in manifest.capabilities:
                continue
            tool = self._manifest_to_mcp_tool(manifest)
            if tool is not None:
                tools.append(tool)
        return tools

    def _manifest_to_mcp_tool(self, manifest: PluginManifest) -> dict[str, Any]:
        """将 PluginManifest 转换为 MCP Tool 描述。"""
        # 构建 inputSchema（MCP 复用 JSON Schema）
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in manifest.params:
            prop: dict[str, Any] = {
                "type": _PARAM_TYPE_MAP.get(param.type, "string"),
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = param.enum
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required

        return {
            "name": f"mcp__plugin__{manifest.id}",
            "description": manifest.description,
            "inputSchema": input_schema,
        }

    async def call_tool(
        self,
        plugin_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """MCP tools/call 转发：调用插件并返回 MCP 标准响应。

        本方法依赖 fusion-cowork 的 PluginLifecycle 执行插件，
        然后将结果包装为 MCP tools/call 响应。
        """
        # 真实实现：通过 desk.runtime 获取 lifecycle 并执行
        # 此处仅返回结构占位，由 Desk 侧 runtime 注入实际调用
        self.desk.log(plugin_id, "INFO", "MCP tools/call 转发", arguments=arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[MCP relay] plugin={plugin_id} arguments={arguments}",
                }
            ],
            "isError": False,
        }

    def gateway_info(self) -> dict[str, Any]:
        """返回 MCP 网关信息（供 Claude Desktop / Claude Code 对接）。"""
        return {
            "transport": "stdio",  # Claude Desktop 默认 stdio
            "port": self.desk.mcp_gateway_port,
            "tools_count": len(self.list_tools()),
            "protocol_version": MCP_PROTOCOL_VERSION,
        }
