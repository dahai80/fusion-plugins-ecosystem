"""MCP JSON-RPC 2.0 请求分发器。

实现 MCP 2026-07-28 协议方法：
- initialize / initialized
- tools/list / tools/call
- resources/list / resources/read
- prompts/list / prompts/get
- ping
- server/discover
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import PluginCapability, PluginRegistry
from fusion_plugins_ecosystem.schema import (
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSIONS_SUPPORTED,
    MCPAnnotations,
    _PARAM_TYPE_MAP,
)

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMESPACE_PREFIX = "mcp__plugin__"


def _extract_plugin_id(tool_name: str) -> str | None:
    """从 MCP 命名空间工具名提取 plugin_id。

    'mcp__plugin__caveman_compress' → 'caveman_compress'
    无命名空间前缀时原样返回（向后兼容）。
    """
    if tool_name.startswith(_MCP_TOOL_NAMESPACE_PREFIX):
        return tool_name[len(_MCP_TOOL_NAMESPACE_PREFIX) :]
    return tool_name if tool_name else None


def _error_response(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err, "id": request_id}


def _result_response(request_id: Any, result: Any) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


class MCPHandler:
    """MCP JSON-RPC 2.0 请求分发器。"""

    PROTOCOL_VERSION = MCP_PROTOCOL_VERSION

    def __init__(
        self,
        registry: PluginRegistry,
        lifecycle: PluginLifecycle | None = None,
        desk: DeskRuntime | None = None,
        config: EcosystemConfig | None = None,
        rate_limit_per_minute: int = 60,
    ) -> None:
        self.registry = registry
        self.lifecycle = lifecycle or PluginLifecycle(registry)
        self.desk = desk or registry.desk
        self.config = config or EcosystemConfig()
        self._initialized = False
        self._client_info: dict[str, Any] = {}
        # C12: 会话管理
        self._sessions: dict[str, dict[str, Any]] = {}
        # C13: 速率限制
        self._rate_limit = rate_limit_per_minute
        self._call_timestamps: dict[str, list[float]] = {}

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """分发 JSON-RPC 请求。"""
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        handler_map = {
            "initialize": self._initialize,
            "initialized": self._on_initialized,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/read": self._resources_read,
            "prompts/list": self._prompts_list,
            "prompts/get": self._prompts_get,
            "server/discover": self._server_discover,
        }

        handler = handler_map.get(method)
        if handler is None:
            logger.warning("jsonrpc: unknown method %r", method)
            return _error_response(request_id, -32601, f"Method not found: {method}")

        try:
            result = await handler(params)
            if request_id is None:
                return None
            return _result_response(request_id, result)
        except Exception as e:
            logger.error("jsonrpc: handler %s error: %s", method, e)
            return _error_response(request_id, -32603, f"Internal error: {e}")

    # ── 协议方法 ──

    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP initialize：协商协议版本和能力。"""
        client_info = params.get("clientInfo", {})
        client_version = params.get("protocolVersion", "")
        self._client_info = client_info

        negotiated = self.PROTOCOL_VERSION
        if client_version in MCP_PROTOCOL_VERSIONS_SUPPORTED:
            negotiated = client_version

        self._initialized = True
        logger.info(
            "jsonrpc: initialize from %s, negotiated=%s",
            client_info.get("name", "unknown"),
            negotiated,
        )

        return {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": False, "listChanged": True},
                "prompts": {"listChanged": True},
            },
            "serverInfo": {
                "name": "fusion-plugins-ecosystem",
                "version": "0.1.0",
            },
        }

    async def _on_initialized(self, params: dict[str, Any]) -> None:
        """MCP initialized：客户端确认初始化完成。"""
        logger.info("jsonrpc: client initialized notification received")

    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP ping：健康检查。"""
        return {}

    async def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP tools/list：列出所有可用的 MCP 工具。"""
        tools = []
        for manifest in self.registry.list():
            if PluginCapability.MCP_TOOL not in manifest.capabilities:
                continue
            tool = self._manifest_to_mcp_tool(manifest)
            if tool is not None:
                tools.append(tool)
        return {"tools": tools}

    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP tools/call：调用插件。"""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        session_id = params.get("_meta", {}).get("sessionId")

        plugin_id = _extract_plugin_id(tool_name)
        if plugin_id is None:
            return {
                "content": [
                    {"type": "text", "text": f"Invalid tool name: {tool_name}"}
                ],
                "isError": True,
            }

        # C13: 速率限制检查
        if not self._check_rate_limit(plugin_id):
            return {
                "content": [{"type": "text", "text": f"Rate limit exceeded for {tool_name}"}],
                "isError": True,
            }

        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        try:
            result = await self.lifecycle.execute(plugin_id, arguments)
            content = self._format_result(result)
            self.desk.log(plugin_id, "INFO", "MCP tools/call completed")
            # C12: 记录会话
            if session_id:
                self._touch_session(session_id, plugin_id)
            return {"content": content, "isError": False}
        except Exception as e:
            logger.error("jsonrpc: tools/call %s error: %s", tool_name, e)
            return {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }

    async def _resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP resources/list：列出资源（当前返回空）。"""
        return {"resources": []}

    async def _resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP resources/read：读取资源。"""
        uri = params.get("uri", "")
        return {
            "contents": [
                {"uri": uri, "text": "Not implemented", "mimeType": "text/plain"}
            ],
        }

    async def _prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP prompts/list：列出提示模板（当前返回空）。"""
        return {"prompts": []}

    async def _prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP prompts/get：获取提示模板。"""
        name = params.get("name", "")
        return {
            "description": f"Prompt {name} not implemented",
            "messages": [],
        }

    async def _server_discover(self, params: dict[str, Any]) -> dict[str, Any]:
        """MCP server/discover：服务器发现（2026-07-28 新增）。"""
        return {
            "name": "fusion-plugins-ecosystem",
            "version": "0.1.0",
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {
                "tools": True,
                "resources": False,
                "prompts": False,
            },
        }

    # ── 内部工具 ──

    # C12: 会话管理
    def _touch_session(self, session_id: str, plugin_id: str) -> None:
        session = self._sessions.setdefault(session_id, {
            "created_at": time.time(),
            "last_active": time.time(),
            "calls": [],
        })
        session["last_active"] = time.time()
        session["calls"].append({"plugin_id": plugin_id, "ts": time.time()})
        if len(session["calls"]) > 1000:
            session["calls"] = session["calls"][-500:]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {"session_id": sid, **s} for sid, s in self._sessions.items()
        ]

    def prune_sessions(self, max_age_seconds: float = 3600) -> int:
        cutoff = time.time() - max_age_seconds
        stale = [sid for sid, s in self._sessions.items() if s["last_active"] < cutoff]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)

    # C13: 速率限制
    def _check_rate_limit(self, plugin_id: str) -> bool:
        now = time.time()
        timestamps = self._call_timestamps.get(plugin_id, [])
        cutoff = now - 60.0
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self._rate_limit:
            logger.warning("jsonrpc: 插件 %s 速率限制触发 (%d/min)", plugin_id, self._rate_limit)
            return False
        timestamps.append(now)
        self._call_timestamps[plugin_id] = timestamps
        return True

    def _manifest_to_mcp_tool(self, manifest: Any) -> dict[str, Any] | None:
        """将 PluginManifest 转为 MCP Tool 描述（2026-07-28 增强）。"""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in manifest.params:
            prop: dict[str, Any] = {
                "type": _PARAM_TYPE_MAP.get(param.type, "string"),
                "description": param.description,
            }
            if param.enum is not None:
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

    def _format_result(self, result: Any) -> list[dict[str, Any]]:
        """将插件返回值格式化为 MCP content 数组。"""
        if isinstance(result, dict):
            if "content" in result and isinstance(result["content"], list):
                return result["content"]
            import json

            text = json.dumps(result, ensure_ascii=False, default=str)
            return [{"type": "text", "text": text}]
        if isinstance(result, str):
            return [{"type": "text", "text": result}]
        if isinstance(result, list):
            return result
        import json

        text = json.dumps(result, ensure_ascii=False, default=str)
        return [{"type": "text", "text": text}]
