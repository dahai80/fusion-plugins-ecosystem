# Plugin → MCP tools exposure.
# Maps to PRD "fusion-cowork exposes an MCP Server; all plugin capabilities
# in the plugin ecosystem auto-register as MCP Tools for Claude to call".
# This module converts PluginManifest into MCP Tool descriptions and
# exposes them via the fusion-cowork MCP gateway. Real MCP protocol framing
# is provided by the fusion-cowork runtime; this module only does
# description generation + call forwarding.

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
    _PARAM_TYPE_MAP,
    MCP_PROTOCOL_VERSION,
    MCPAnnotations,
)

logger = logging.getLogger(__name__)


# MCP tool description template (based on the MCP spec tools/list response item)
_MCP_TOOL_TEMPLATE = {
    "jsonrpc": "2.0",
    "protocolVersion": MCP_PROTOCOL_VERSION,
}

_MCP_TOOL_NAMESPACE_PREFIX = "mcp__plugin__"


def manifest_to_mcp_tool(manifest: PluginManifest) -> dict[str, Any] | None:
    # Convert a PluginManifest into an MCP tool description (SSOT, reused
    # by all adapters).
    # Unified output: enum is always converted to list (valid JSON array),
    # includes title/annotations/outputSchema (MCP 2026-07-28). Prevents
    # schema divergence from jsonrpc / mcp_exporter each generating their own.
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in manifest.params:
        prop: dict[str, Any] = {
            "type": _PARAM_TYPE_MAP.get(param.type, "string"),
            "description": param.description,
        }
        if param.enum is not None:
            # enum is always output as list; tuple is invalid in JSON
            prop["enum"] = list(param.enum)
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

    annotations = MCPAnnotations()
    if hasattr(manifest, "mcp_annotations") and manifest.mcp_annotations:
        annotations = manifest.mcp_annotations

    tool: dict[str, Any] = {
        "name": f"{_MCP_TOOL_NAMESPACE_PREFIX}{manifest.id}",
        "title": manifest.name,
        "description": manifest.description,
        "inputSchema": input_schema,
        "annotations": annotations.to_dict(),
    }

    if hasattr(manifest, "output_schema") and manifest.output_schema:
        tool["outputSchema"] = manifest.output_schema

    return tool


class MCPExporter:
    # Plugin → MCP tools exporter.
    # Usage:
    #     exporter = MCPExporter(registry, desk)
    #     tools = exporter.list_tools()           # MCP tools/list response
    #     result = await exporter.call_tool(...)  # MCP tools/call forward

    def __init__(
        self,
        registry: PluginRegistry,
        desk: DeskRuntime | None = None,
    ) -> None:
        self.registry = registry
        self.desk = desk or registry.desk

    def list_tools(self) -> list[dict[str, Any]]:
        # Build the MCP tools/list response item list.
        # Only plugins with the MCP_TOOL capability are exposed.
        tools: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            if PluginCapability.MCP_TOOL not in manifest.capabilities:
                continue
            tool = self._manifest_to_mcp_tool(manifest)
            if tool is not None:
                tools.append(tool)
        return tools

    def _manifest_to_mcp_tool(self, manifest: PluginManifest) -> dict[str, Any]:
        # Convert a PluginManifest into an MCP tool description (delegates
        # to SSOT manifest_to_mcp_tool).
        return manifest_to_mcp_tool(manifest)

    async def call_tool(
        self,
        plugin_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        # MCP tools/call forward: invoke the plugin and return the standard
        # MCP response.
        # Relies on fusion-cowork's PluginLifecycle to execute the plugin,
        # then wraps the result into an MCP tools/call response.
        # Real implementation: fetch the lifecycle via desk.runtime and execute
        # This only returns a structural placeholder; the Desk-side runtime
        # injects the actual call.
        # i18n: user-facing
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
        # Return MCP gateway info (for Claude Desktop / Claude Code onboarding).
        return {
            "transport": "stdio",  # Claude Desktop defaults to stdio
            "port": self.desk.mcp_gateway_port,
            "tools_count": len(self.list_tools()),
            "protocol_version": MCP_PROTOCOL_VERSION,
        }
