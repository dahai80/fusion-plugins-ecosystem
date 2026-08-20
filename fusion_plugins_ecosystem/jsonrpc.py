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

from fusion_plugins_ecosystem import __version__ as _PKG_VERSION
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
from fusion_plugins_ecosystem.token_meter import TokenMeter

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMESPACE_PREFIX = "mcp__plugin__"
# RPC 触发的淘汰操作最小保留时长（秒），防止全量清空审计/会话记录
_PRUNE_MIN_AGE = 60.0

# Studio EcosystemConfig 期望的 7 个字段名 → 后端 EcosystemConfig 字段映射
# 后端字段名与 Studio 不一致，这里做适配投影
_STUDIO_CONFIG_KEYS = {
    "sandbox_mode": "sandbox_default_mode",
    "auto_update": "auto_export_claude_skill",
    "max_concurrent_plugins": "max_auto_restart",
    "log_level": "mcp_transport",
    "token_budget": "max_token_records",
    "vram_limit_mb": "mcp_port",
    "mcp_enabled": "enable_claude_mcp",
}


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
        token_meter: TokenMeter | None = None,
        rate_limit_per_minute: int = 60,
    ) -> None:
        self.registry = registry
        self.lifecycle = lifecycle or PluginLifecycle(registry)
        self.desk = desk or registry.desk
        self.config = config or EcosystemConfig()
        self.token_meter: TokenMeter = token_meter or TokenMeter(self.desk)
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
            # ── Studio 集成面板 plugins/* 方法（dict 信封）──
            "plugins.ping": self._plugins_ping,
            "plugins/list": self._plugins_list,
            "plugins/install": self._plugins_install,
            "plugins/uninstall": self._plugins_uninstall,
            "plugins/config.get": self._plugins_config_get,
            "plugins/config.set": self._plugins_config_set,
            "plugins/states": self._plugins_states,
            "plugins/state.get": self._plugins_state_get,
            "plugins/state.list": self._plugins_state_list,
            "plugins/token.records": self._plugins_token_records,
            "plugins/token.prune": self._plugins_token_prune,
            "plugins/vram.usage": self._plugins_vram_usage,
            "plugins/logs.stream": self._plugins_logs_stream,
            "plugins/mcp.sessions": self._plugins_mcp_sessions,
            "plugins/mcp.sessions.prune": self._plugins_mcp_sessions_prune,
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
                "version": _PKG_VERSION,
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
                "content": [
                    {"type": "text", "text": f"Rate limit exceeded for {tool_name}"}
                ],
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
            "version": _PKG_VERSION,
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {
                "tools": True,
                "resources": False,
                "prompts": False,
            },
        }

    # ── Studio 集成面板 plugins/* 方法 ──
    # 信封格式遵循 PluginBridge.swift 读取约定：dict result + 具名键

    async def _plugins_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins.ping：Studio 健康检查。"""
        return {"pong": True}

    async def _plugins_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/list：列出全部插件（供 Studio 插件目录）。

        Studio PluginListItem.fromDict 期望：id/name/category/version/description/
        author/enabled/installed。
        """
        category = params.get("category")
        manifests = (
            self.registry.list_as_dicts(category=category)
            if category
            else (self.registry.list_as_dicts())
        )
        items = []
        for m in manifests:
            inst = self.lifecycle._instances.get(m["id"])
            items.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "category": m["category"],
                    "version": m["version"],
                    "description": m["description"],
                    "author": None,
                    "enabled": inst is not None and inst.state.value == "enabled",
                    "installed": m["id"] in self.lifecycle._instances,
                }
            )
        return {"plugins": items}

    async def _plugins_install(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/install：加载并启用插件。"""
        plugin_id = params.get("plugin_id", "")
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return {"ok": False, "error": f"插件 {plugin_id!r} 未注册"}
        await self.lifecycle.enable(plugin_id)
        return {"ok": True}

    async def _plugins_uninstall(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/uninstall：禁用并卸载插件（保留注册）。"""
        plugin_id = params.get("plugin_id", "")
        await self.lifecycle.disable(plugin_id)
        self.lifecycle.unload(plugin_id)
        return {"ok": True}

    async def _plugins_config_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/config.get：返回配置（投影为 Studio 7 字段命名）。"""
        return self._config_to_studio_dict(self.config)

    async def _plugins_config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/config.set：设置单个配置项。

        params 即 {key: value} 单键值对（PluginBridge.swift 约定）。
        支持后端原字段名或 Studio 投影名。
        经 EcosystemConfig.from_dict 校验类型/范围/枚举，非法值拒绝并报错。
        """
        if not params:
            return {"ok": False, "error": "空参数"}
        key, value = next(iter(params.items()))
        backend_key = _STUDIO_CONFIG_KEYS.get(key, key)
        if backend_key not in self.config.to_dict():
            logger.warning("jsonrpc: config.set 拒绝未知字段 %r", key)
            return {"ok": False, "error": f"未知字段 {key!r}"}
        probe, warnings = EcosystemConfig.from_dict({backend_key: value})
        if warnings:
            logger.warning(
                "jsonrpc: config.set %s 校验失败: %s", key, "; ".join(warnings)
            )
            return {"ok": False, "error": "; ".join(warnings)}
        setattr(self.config, backend_key, getattr(probe, backend_key))
        self.config._notify_change(backend_key, None, getattr(probe, backend_key))
        return {"ok": True}

    async def _plugins_states(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/states：返回全部插件状态快照（供 Studio 状态面板）。"""
        return {"states": self.lifecycle.list_states()}

    async def _plugins_state_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/state.get：返回单个插件状态快照（dict 信封）。"""
        plugin_id = params.get("plugin_id", "")
        state = self.lifecycle.get_state(plugin_id)
        if state is None:
            return {
                "id": plugin_id,
                "plugin_id": plugin_id,
                "state": "unknown",
            }
        return state

    async def _plugins_state_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/state.list：按状态过滤返回插件快照列表。"""
        state_str = params.get("state", "")
        try:
            from fusion_plugins_ecosystem.lifecycle import PluginState

            state_enum = PluginState(state_str)
        except ValueError:
            return {"plugins": []}
        return {"plugins": self.lifecycle.list_by_state(state_enum)}

    async def _plugins_token_records(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/token.records：返回 token 记录（Studio 字段命名）。"""
        plugin_id = params.get("plugin_id")
        records = (
            self.token_meter.records_for(plugin_id)
            if plugin_id
            else self.token_meter.all_records()
        )
        items = [
            {
                "id": f"{r.plugin_id}-{r.timestamp}",
                "plugin_id": r.plugin_id,
                "prompt_tokens": r.input_tokens,
                "completion_tokens": r.output_tokens,
                "total_tokens": r.total_tokens,
                "timestamp": str(int(r.timestamp * 1000)),
                "model": r.metadata.get("model") if r.metadata else None,
            }
            for r in records
        ]
        return {"records": items}

    async def _plugins_token_prune(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/token.prune：按时间淘汰旧 token 记录。

        max_age_seconds 下限 _PRUNE_MIN_AGE 秒，防止 RPC 全量清空审计记录。
        """
        max_age = params.get("max_age_seconds", 3600)
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = 3600.0
        max_age = max(max_age, _PRUNE_MIN_AGE)
        self.token_meter.prune(max_age_seconds=max_age)
        return {"ok": True}

    async def _plugins_vram_usage(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/vram.usage：返回显存使用（Studio 结构 total/used/free/by_plugin）。"""
        allocs = self.desk.vram_usage()
        total = self.desk.vram_total_mb
        used = sum(allocs.values())
        by_plugin = [
            {
                "id": pid,
                "plugin_id": pid,
                "allocated_mb": mb,
                "peak_mb": mb,
            }
            for pid, mb in allocs.items()
        ]
        return {
            "total_mb": total,
            "used_mb": used,
            "free_mb": max(0, total - used) if total > 0 else 0,
            "by_plugin": by_plugin,
        }

    async def _plugins_logs_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/logs.stream：返回日志缓冲区（Studio PluginLogEntry 命名）。

        Studio 要求 id 为 String，后端缓冲区 id 为 int，这里转字符串。
        """
        plugin_id = params.get("plugin_id")
        level = params.get("level")
        entries = self.desk.get_logs(plugin_id=plugin_id, level=level)
        items = [
            {
                "id": str(e["id"]),
                "plugin_id": e["plugin_id"],
                "level": e["level"],
                "message": e["message"],
                "timestamp": e["timestamp"],
            }
            for e in entries
        ]
        return {"entries": items}

    async def _plugins_mcp_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/mcp.sessions：返回 MCP 会话列表（Studio MCPSession 命名）。"""
        sessions = self.list_sessions()
        items = [
            {
                "id": s["session_id"],
                "session_id": s["session_id"],
                "plugin_id": "",
                "server": "fusion-plugins-ecosystem",
                "status": "connected",
                "tool_count": len(s.get("calls", [])),
                "connected_at": str(int(s.get("created_at", 0) * 1000)),
            }
            for s in sessions
        ]
        return {"sessions": items}

    async def _plugins_mcp_sessions_prune(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """plugins/mcp.sessions.prune：按时间淘汰过期 MCP 会话。

        max_age_seconds 下限 _PRUNE_MIN_AGE 秒，防止 RPC 全量清空。
        """
        max_age = params.get("max_age_seconds", 3600)
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = 3600.0
        max_age = max(max_age, _PRUNE_MIN_AGE)
        self.prune_sessions(max_age_seconds=max_age)
        return {"ok": True}

    # ── 内部工具 ──

    def _config_to_studio_dict(self, config: EcosystemConfig) -> dict[str, Any]:
        """将后端 EcosystemConfig 投影为 Studio 期望的 7 字段命名。"""
        backend = config.to_dict()
        result: dict[str, Any] = {}
        for studio_key, backend_key in _STUDIO_CONFIG_KEYS.items():
            result[studio_key] = backend.get(backend_key)
        return result

    # C12: 会话管理
    def _touch_session(self, session_id: str, plugin_id: str) -> None:
        session = self._sessions.setdefault(
            session_id,
            {
                "created_at": time.time(),
                "last_active": time.time(),
                "calls": [],
            },
        )
        session["last_active"] = time.time()
        session["calls"].append({"plugin_id": plugin_id, "ts": time.time()})
        if len(session["calls"]) > 1000:
            session["calls"] = session["calls"][-500:]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [{"session_id": sid, **s} for sid, s in self._sessions.items()]

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
            logger.warning(
                "jsonrpc: 插件 %s 速率限制触发 (%d/min)", plugin_id, self._rate_limit
            )
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
