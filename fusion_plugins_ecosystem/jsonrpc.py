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

import json
import logging
import re
import time
from collections import deque
from typing import Any

from fusion_plugins_ecosystem import __version__ as _PKG_VERSION
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
from fusion_plugins_ecosystem.mcp_exporter import manifest_to_mcp_tool
from fusion_plugins_ecosystem.registry import PluginCapability, PluginRegistry
from fusion_plugins_ecosystem.schema import (
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSIONS_SUPPORTED,
)
from fusion_plugins_ecosystem.token_meter import TokenMeter

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMESPACE_PREFIX = "mcp__plugin__"
# RPC 触发的淘汰操作最小保留时长（秒），防止全量清空审计/会话记录
_PRUNE_MIN_AGE = 60.0
# 最大并发会话数，超出 LRU 淘汰（P0-5）
_MAX_SESSIONS = 256
# sessionId 合法格式（防止伪造无界 ID 撑爆内存）
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# Studio EcosystemConfig 期望的 7 个字段名 → 后端 EcosystemConfig 字段映射
# A3 修复：log_level/vram_limit_mb/max_concurrent_plugins 已补齐真实后端字段，
# 不再错误映射到 mcp_transport/mcp_port/max_auto_restart（语义反转 bug）
_STUDIO_CONFIG_KEYS = {
    "sandbox_mode": "sandbox_default_mode",
    "auto_update": "auto_export_claude_skill",
    "max_concurrent_plugins": "max_concurrent_plugins",
    "log_level": "log_level",
    "token_budget": "max_token_records",
    "vram_limit_mb": "vram_limit_mb",
    "mcp_enabled": "enable_claude_mcp",
}

# P1-5：安全敏感字段经 RPC 只读。变更沙箱隔离强度、监听地址、凭据存储开关
# 等属部署期决策，不应经运行时 RPC 被未授权客户端翻转。
_RPC_READONLY_CONFIG_KEYS = frozenset(
    {
        "sandbox_default_mode",
        "mcp_host",
        "mcp_port",
        "mcp_transport",
        "enable_volcengine_claude_plan",
        "token_persist_path",
    }
)


def _extract_plugin_id(tool_name: str) -> str | None:
    """从 MCP 命名空间工具名提取 plugin_id。

    'mcp__plugin__caveman_compress' → 'caveman_compress'
    无命名空间前缀时原样返回（向后兼容）。
    """
    if tool_name.startswith(_MCP_TOOL_NAMESPACE_PREFIX):
        return tool_name[len(_MCP_TOOL_NAMESPACE_PREFIX) :]
    return tool_name if tool_name else None


# P2-2：plugin_id 字符集白名单，防换行/控制字符伪造日志行（日志注入）
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _valid_plugin_id(plugin_id: str | None) -> bool:
    """校验 plugin_id 格式：仅字母数字/下划线/点/连字符，长度 1-128。"""
    return bool(plugin_id) and bool(_PLUGIN_ID_RE.match(plugin_id))


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
        self.config = config or EcosystemConfig()
        # A2/A5：lifecycle 与 token_meter 默认构造时注入 config，避免
        # 超时/重启/心跳阈值与 token 计量配置脱钩（调用方传显式实例则优先用之）
        self.lifecycle = lifecycle or PluginLifecycle(registry, config=self.config)
        self.desk = desk or registry.desk
        self.token_meter: TokenMeter = token_meter or TokenMeter(
            self.desk,
            max_records=self.config.max_token_records,
            persist_path=self.config.token_persist_path,
        )
        self._initialized = False
        self._client_info: dict[str, Any] = {}
        # C12/C13: 会话 + 速率限制状态挂在 desk 上（R5），多个 MCPHandler 共用
        # 同一 DeskRuntime 时以 desk 为单一来源，避免 per-handler 计数放大限流上限。
        # 单 handler 场景（每个 handler 独立 desk）行为不变。
        self._sessions = self.desk._mcp_sessions
        self._call_timestamps = self.desk._mcp_call_timestamps
        self._state_lock = self.desk._mcp_state_lock
        # C13: 速率上限仍为 per-handler 配置（不同入口可设不同上限）
        self._rate_limit = rate_limit_per_minute

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
            # P1-7：对外仅返回通用错误码，详情写日志，避免异常文本泄露
            # 内部模块路径/文件系统结构/变量值等敏感信息。
            logger.error("jsonrpc: handler %s error: %s", method, e, exc_info=True)
            return _error_response(request_id, -32603, "Internal error")

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
            # P1-7：对外返回通用错误消息，原始异常详情写日志，防信息泄露
            logger.error(
                "jsonrpc: tools/call %s error: %s", tool_name, e, exc_info=True
            )
            return {
                "content": [
                    {"type": "text", "text": f"Plugin execution failed: {type(e).__name__}"}
                ],
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
        """plugins/install：加载并启用插件。

        A4：安装结果持久化到 config_center，进程重启后可由 server.start() 恢复。
        """
        plugin_id = params.get("plugin_id", "")
        # P2-2：plugin_id 格式校验，防日志注入
        if not _valid_plugin_id(plugin_id):
            return {"ok": False, "error": "非法 plugin_id"}
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return {"ok": False, "error": f"插件 {plugin_id!r} 未注册"}
        try:
            await self.lifecycle.enable(plugin_id)
        except Exception as exc:
            logger.error("jsonrpc: install %s 失败: %s", plugin_id, exc)
            return {"ok": False, "error": str(exc)}
        self._persist_installed(plugin_id, True)
        return {"ok": True}

    async def _plugins_uninstall(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/uninstall：禁用并卸载插件（保留注册）。

        A4：卸载同步从持久化已安装集合移除。
        """
        plugin_id = params.get("plugin_id", "")
        # P2-2：plugin_id 格式校验
        if not _valid_plugin_id(plugin_id):
            return {"ok": False, "error": "非法 plugin_id"}
        await self.lifecycle.disable(plugin_id)
        self.lifecycle.unload(plugin_id)
        self._persist_installed(plugin_id, False)
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
        # P1-5：安全敏感字段经 RPC 只读，防止未授权翻转沙箱模式/监听地址等。
        # 变更需在部署期（环境变量/配置文件）完成，不经运行时 RPC。
        if backend_key in _RPC_READONLY_CONFIG_KEYS:
            logger.warning(
                "jsonrpc: config.set 拒绝敏感字段 %r（RPC 只读）", backend_key
            )
            return {
                "ok": False,
                "error": f"字段 {key!r} 为安全敏感项，不可经 RPC 修改",
            }
        probe, warnings = EcosystemConfig.from_dict({backend_key: value})
        if warnings:
            logger.warning(
                "jsonrpc: config.set %s 校验失败: %s", key, "; ".join(warnings)
            )
            return {"ok": False, "error": "; ".join(warnings)}
        setattr(self.config, backend_key, getattr(probe, backend_key))
        self.config._notify_change(backend_key, None, getattr(probe, backend_key))
        # A4：配置变更持久化到 config_center，进程重启后可恢复
        self._persist_config(backend_key, getattr(probe, backend_key))
        return {"ok": True}

    async def _plugins_states(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/states：返回全部插件状态快照（供 Studio 状态面板）。"""
        return {"states": self.lifecycle.list_states()}

    async def _plugins_state_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """plugins/state.get：返回单个插件状态快照（dict 信封）。"""
        plugin_id = params.get("plugin_id", "")
        # P2-2：plugin_id 格式校验
        if not _valid_plugin_id(plugin_id):
            return {"id": plugin_id, "plugin_id": plugin_id, "state": "unknown"}
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

    # ── A4 持久化辅助：config_center 缺失时静默降级（standalone/测试）──

    _PERSIST_CONFIG_PREFIX = "plugin_ecosystem.config."
    _PERSIST_INSTALLED_KEY = "plugin_ecosystem.installed_ids"

    def _persist_config(self, key: str, value: Any) -> None:
        """单字段配置变更持久化到 Desk config_center。"""
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            cc.set(self._PERSIST_CONFIG_PREFIX + key, value)
        except Exception as exc:
            logger.warning("jsonrpc: 配置持久化 %s 失败: %s", key, exc)

    def persist_full_config(self) -> None:
        """全量配置持久化（启动恢复前落盘调用）。"""
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            for key, value in self.config.to_dict().items():
                cc.set(self._PERSIST_CONFIG_PREFIX + key, value)
        except Exception as exc:
            logger.warning("jsonrpc: 全量配置持久化失败: %s", exc)

    def _persist_installed(self, plugin_id: str, installed: bool) -> None:
        """更新持久化已安装插件 ID 集合。"""
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            raw = cc.get(self._PERSIST_INSTALLED_KEY) or []
            ids = set(raw) if isinstance(raw, list) else set()
            ids.add(plugin_id) if installed else ids.discard(plugin_id)
            cc.set(self._PERSIST_INSTALLED_KEY, sorted(ids))
        except Exception as exc:
            logger.warning("jsonrpc: 安装态持久化 %s 失败: %s", plugin_id, exc)

    def restore_installed(self) -> list[str]:
        """从 config_center 读取持久化的已安装插件 ID 列表（server 启动恢复用）。"""
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return []
        try:
            raw = cc.get(self._PERSIST_INSTALLED_KEY)
            return list(raw) if isinstance(raw, list) else []
        except Exception:
            return []

    def restore_config(self) -> list[str]:
        """从 config_center 读取持久化配置并合并到 self.config，返回恢复的键列表。"""
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return []
        restored: list[str] = []
        patch: dict[str, Any] = {}
        try:
            for key in self.config.to_dict():
                stored = cc.get(self._PERSIST_CONFIG_PREFIX + key)
                if stored is not None:
                    patch[key] = stored
                    restored.append(key)
        except Exception as exc:
            logger.warning("jsonrpc: 配置恢复失败: %s", exc)
            return []
        if patch:
            merged, warnings = EcosystemConfig.from_dict(
                {**self.config.to_dict(), **patch}
            )
            self.config = merged
            if warnings:
                logger.warning("jsonrpc: 恢复配置校验告警: %s", "; ".join(warnings))
            # P2-1：恢复的配置须同步应用到 lifecycle 与 token_meter，
            # 否则 server 启动时二者仍绑旧 config，超时/心跳/token 持久化阈值静默失效。
            self._apply_config_to_deps()
        return restored

    def _apply_config_to_deps(self) -> None:
        """把 self.config 同步到 lifecycle 与 token_meter（配置恢复后调用）。

        lifecycle 通过 getter 实时读 config，仅需替换其 config 引用；
        token_meter 的 max_records/persist_path 为构造期快照，需显式刷新。
        """
        try:
            if self.lifecycle is not None:
                self.lifecycle.config = self.config
        except Exception as exc:
            logger.warning("jsonrpc: 同步 config 到 lifecycle 失败: %s", exc)
        try:
            if self.token_meter is not None:
                self.token_meter._max_records = self.config.max_token_records
                self.token_meter._persist_path = self.config.token_persist_path
        except Exception as exc:
            logger.warning("jsonrpc: 同步 config 到 token_meter 失败: %s", exc)

    # C12: 会话管理
    def _touch_session(self, session_id: str, plugin_id: str) -> None:
        # sessionId 格式校验，拒绝伪造无界 ID（P0-5）
        if not _SESSION_ID_RE.match(session_id):
            logger.warning("jsonrpc: 拒绝非法 sessionId %r", session_id)
            return
        # R5：共享状态加锁，多 handler 并发 touch 不丢失/错乱会话
        with self._state_lock:
            # 会话数上限 + LRU 淘汰，防止内存耗尽（P0-5）
            if len(self._sessions) >= _MAX_SESSIONS and session_id not in self._sessions:
                self._evict_oldest_session()
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

    def _evict_oldest_session(self) -> None:
        """淘汰最久未活跃的会话（LRU）。调用方须持 _state_lock。"""
        if not self._sessions:
            return
        oldest_id = min(self._sessions, key=lambda s: self._sessions[s]["last_active"])
        del self._sessions[oldest_id]
        logger.info("jsonrpc: 会话数达上限，LRU 淘汰 %s", oldest_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [{"session_id": sid, **s} for sid, s in self._sessions.items()]

    def prune_sessions(self, max_age_seconds: float = 3600) -> int:
        with self._state_lock:
            cutoff = time.time() - max_age_seconds
            stale = [
                sid
                for sid, s in self._sessions.items()
                if s["last_active"] < cutoff
            ]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)

    # C13: 速率限制
    def _check_rate_limit(self, plugin_id: str) -> bool:
        now = time.time()
        cutoff = now - 60.0
        # R5：共享时间戳表加锁，多 handler 并发计数不漏判/超判
        with self._state_lock:
            timestamps = self._call_timestamps.get(plugin_id)
            if timestamps is None:
                timestamps = deque()
                self._call_timestamps[plugin_id] = timestamps
            # 从左侧淘汰过期时间戳（已按时间顺序入队，左侧最旧）
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._rate_limit:
                logger.warning(
                    "jsonrpc: 插件 %s 速率限制触发 (%d/min)",
                    plugin_id,
                    self._rate_limit,
                )
                return False
            timestamps.append(now)
            return True

    def _manifest_to_mcp_tool(self, manifest: Any) -> dict[str, Any] | None:
        """将 PluginManifest 转为 MCP Tool 描述（委托 SSOT，避免双份分叉）。"""
        return manifest_to_mcp_tool(manifest)

    def _format_result(self, result: Any) -> list[dict[str, Any]]:
        """将插件返回值格式化为 MCP content 数组。"""
        if isinstance(result, dict):
            if "content" in result and isinstance(result["content"], list):
                return result["content"]
            text = json.dumps(result, ensure_ascii=False, default=str)
            return [{"type": "text", "text": text}]
        if isinstance(result, str):
            return [{"type": "text", "text": result}]
        if isinstance(result, list):
            return result
        text = json.dumps(result, ensure_ascii=False, default=str)
        return [{"type": "text", "text": text}]
