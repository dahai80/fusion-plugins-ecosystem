# MCP JSON-RPC 2.0 request dispatcher.
# Implements the MCP 2026-07-28 protocol methods:
# - initialize / initialized
# - tools/list / tools/call
# - resources/list / resources/read
# - prompts/list / prompts/get
# - ping
# - server/discover

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
# Minimum retention for RPC-triggered prune operations (seconds), preventing
# a full wipe of audit/session records
_PRUNE_MIN_AGE = 60.0
# Max concurrent sessions; beyond this LRU eviction applies (P0-5)
_MAX_SESSIONS = 256
# Valid sessionId format (prevents forged unbounded IDs exhausting memory)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# Studio EcosystemConfig expected 7 field names → backend EcosystemConfig field map
# A3 fix: log_level/vram_limit_mb/max_concurrent_plugins now map to the real
# backend fields, no longer mis-mapped to mcp_transport/mcp_port/
# max_auto_restart (inverted-semantics bug)
_STUDIO_CONFIG_KEYS = {
    "sandbox_mode": "sandbox_default_mode",
    "auto_update": "auto_export_claude_skill",
    "max_concurrent_plugins": "max_concurrent_plugins",
    "log_level": "log_level",
    "token_budget": "max_token_records",
    "vram_limit_mb": "vram_limit_mb",
    "mcp_enabled": "enable_claude_mcp",
}

# P1-5: security-sensitive fields are read-only over RPC. Changing sandbox
# isolation strength, listen address, credential storage toggles, etc. are
# deployment-time decisions and must not be flipped by unauthorized clients
# via runtime RPC.
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


# Extract the plugin_id from an MCP namespaced tool name.
# 'mcp__plugin__caveman_compress' → 'caveman_compress'
# Returns the input unchanged when there is no namespace prefix (back-compat).
def _extract_plugin_id(tool_name: str) -> str | None:
    if tool_name.startswith(_MCP_TOOL_NAMESPACE_PREFIX):
        return tool_name[len(_MCP_TOOL_NAMESPACE_PREFIX) :]
    return tool_name if tool_name else None


# P2-2: plugin_id character whitelist, preventing newline/control characters
# from forging log lines (log injection)
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


# Validate plugin_id format: alphanumerics/underscore/dot/hyphen only, length 1-128.
def _valid_plugin_id(plugin_id: str | None) -> bool:
    return bool(plugin_id) and bool(_PLUGIN_ID_RE.match(plugin_id))


# Build a JSON-RPC error response.
def _error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err, "id": request_id}


# Build a JSON-RPC success response.
def _result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


class MCPHandler:
    # MCP JSON-RPC 2.0 request dispatcher.
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
        # A2/A5: inject config into lifecycle and token_meter when constructed
        # by default, so timeout/restart/heartbeat thresholds and token metering
        # config stay coupled (explicit instances passed by the caller win)
        self.lifecycle = lifecycle or PluginLifecycle(registry, config=self.config)
        self.desk = desk or registry.desk
        self.token_meter: TokenMeter = token_meter or TokenMeter(
            self.desk,
            max_records=self.config.max_token_records,
            persist_path=self.config.token_persist_path,
        )
        self._initialized = False
        self._client_info: dict[str, Any] = {}
        # C12/C13: session + rate limit state hangs off desk (R5); when several
        # MCPHandlers share one DeskRuntime, desk is the single source of truth,
        # avoiding per-handler counting that multiplies the rate limit cap.
        # Single-handler scenarios (each handler with its own desk) unchanged.
        self._sessions = self.desk._mcp_sessions
        self._call_timestamps = self.desk._mcp_call_timestamps
        self._state_lock = self.desk._mcp_state_lock
        # C13: the rate cap remains per-handler config (different entry points
        # may set different caps)
        self._rate_limit = rate_limit_per_minute

    # Dispatch a JSON-RPC request.
    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
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
            # ── Studio integration panel plugins/* methods (dict envelope) ──
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
            # P1-7: return only a generic error code externally; write details
            # to logs to avoid leaking internal module paths/filesystem
            # layout/variable values.
            logger.error("jsonrpc: handler %s error: %s", method, e, exc_info=True)
            # P2-9: handler errors funnel into the desk ring buffer (no
            # exception details recorded to prevent leakage)
            try:
                self.desk.infra_log("jsonrpc", "ERROR", f"handler {method} error: {type(e).__name__}")
            except Exception:
                pass
            return _error_response(request_id, -32603, "Internal error")

    # ── Protocol methods ──

    # MCP initialize: negotiate protocol version and capabilities.
    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
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

    # MCP initialized: client confirms initialization complete.
    async def _on_initialized(self, params: dict[str, Any]) -> None:
        logger.info("jsonrpc: client initialized notification received")

    # MCP ping: health check.
    async def _ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    # MCP tools/list: list all available MCP tools.
    async def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        tools = []
        for manifest in self.registry.list():
            if PluginCapability.MCP_TOOL not in manifest.capabilities:
                continue
            tool = self._manifest_to_mcp_tool(manifest)
            if tool is not None:
                tools.append(tool)
        return {"tools": tools}

    # MCP tools/call: invoke a plugin.
    async def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        session_id = params.get("_meta", {}).get("sessionId")
        # P2-5: take the idempotency key from _meta.idempotencyKey (guards
        # against duplicate execution on client reconnect retries) and pass it
        # through to lifecycle.execute's idempotency cache. It does not enter
        # the plugin arguments.
        idem_key = params.get("_meta", {}).get("idempotencyKey")
        if idem_key:
            arguments = {**arguments, "_idempotency_key": str(idem_key)}

        plugin_id = _extract_plugin_id(tool_name)
        if plugin_id is None:
            return {
                "content": [{"type": "text", "text": f"Invalid tool name: {tool_name}"}],
                "isError": True,
            }

        # C13: rate limit check
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
            self.desk.metrics.counter("mcp_tool_calls_total", "MCP tools/call calls total (bucketed by result)").inc(
                plugin=plugin_id, status="success"
            )
            # C12: record the session
            if session_id:
                self._touch_session(session_id, plugin_id)
            return {"content": content, "isError": False}
        except Exception as e:
            # P1-7: return a generic error message externally; log the original
            # exception details to prevent info leakage
            logger.error("jsonrpc: tools/call %s error: %s", tool_name, e, exc_info=True)
            # P2-9: tools/call execution errors funnel into the desk ring buffer
            try:
                self.desk.infra_log(
                    "jsonrpc",
                    "ERROR",
                    f"tools/call {tool_name} error: {type(e).__name__}",
                )
            except Exception:
                pass
            self.desk.metrics.counter("mcp_tool_calls_total", "MCP tools/call calls total (bucketed by result)").inc(
                plugin=plugin_id, status="error"
            )
            self.desk.metrics.counter("mcp_tool_errors_total", "MCP tools/call errors total (bucketed by type)").inc(
                plugin=plugin_id, kind=type(e).__name__
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Plugin execution failed: {type(e).__name__}",
                    }
                ],
                "isError": True,
            }

    # MCP resources/list: list resources (currently empty).
    async def _resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"resources": []}

    # MCP resources/read: read a resource.
    async def _resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri", "")
        return {
            "contents": [{"uri": uri, "text": "Not implemented", "mimeType": "text/plain"}],
        }

    # MCP prompts/list: list prompt templates (currently empty).
    async def _prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"prompts": []}

    # MCP prompts/get: fetch a prompt template.
    async def _prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        return {
            "description": f"Prompt {name} not implemented",
            "messages": [],
        }

    # MCP server/discover: server discovery (added in 2026-07-28).
    async def _server_discover(self, params: dict[str, Any]) -> dict[str, Any]:
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

    # ── Studio integration panel plugins/* methods ──
    # Envelope format follows the PluginBridge.swift reading convention:
    # dict result + named keys

    # plugins.ping: Studio health check.
    async def _plugins_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    # plugins/list: list all plugins (feeds the Studio plugin catalog).
    # Studio PluginListItem.fromDict expects: id/name/category/version/
    # description/author/enabled/installed.
    async def _plugins_list(self, params: dict[str, Any]) -> dict[str, Any]:
        category = params.get("category")
        manifests = self.registry.list_as_dicts(category=category) if category else (self.registry.list_as_dicts())
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

    # plugins/install: load and enable a plugin.
    # A4: install results persist to config_center and can be restored by
    # server.start() after a process restart.
    async def _plugins_install(self, params: dict[str, Any]) -> dict[str, Any]:
        plugin_id = params.get("plugin_id", "")
        # P2-2: plugin_id format validation, prevents log injection
        if not _valid_plugin_id(plugin_id):
            return {"ok": False, "error": "invalid plugin_id"}
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return {"ok": False, "error": f"plugin {plugin_id!r} not registered"}
        try:
            await self.lifecycle.enable(plugin_id)
        except Exception as exc:
            logger.error("jsonrpc: install %s failed: %s", plugin_id, exc)
            return {"ok": False, "error": str(exc)}
        self._persist_installed(plugin_id, True)
        return {"ok": True}

    # plugins/uninstall: disable and unload a plugin (registration retained).
    # A4: uninstall removes the id from the persisted installed set in sync.
    async def _plugins_uninstall(self, params: dict[str, Any]) -> dict[str, Any]:
        plugin_id = params.get("plugin_id", "")
        # P2-2: plugin_id format validation
        if not _valid_plugin_id(plugin_id):
            return {"ok": False, "error": "invalid plugin_id"}
        await self.lifecycle.disable(plugin_id)
        self.lifecycle.unload(plugin_id)
        self._persist_installed(plugin_id, False)
        return {"ok": True}

    # plugins/config.get: return config (projected to Studio's 7 field names).
    async def _plugins_config_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._config_to_studio_dict(self.config)

    # plugins/config.set: set a single config item.
    # params is a {key: value} single key-value pair (PluginBridge.swift convention).
    # Accepts backend original field names or Studio projected names.
    # Validated via EcosystemConfig.from_dict for type/range/enum; invalid
    # values are rejected with an error.
    async def _plugins_config_set(self, params: dict[str, Any]) -> dict[str, Any]:
        if not params:
            return {"ok": False, "error": "empty params"}
        key, value = next(iter(params.items()))
        backend_key = _STUDIO_CONFIG_KEYS.get(key, key)
        if backend_key not in self.config.to_dict():
            logger.warning("jsonrpc: config.set rejecting unknown field %r", key)
            return {"ok": False, "error": f"unknown field {key!r}"}
        # P1-5: security-sensitive fields are read-only over RPC, preventing
        # unauthorized flips of sandbox mode/listen address etc.
        # Changes must be made at deployment time (env vars/config files), not
        # via runtime RPC.
        if backend_key in _RPC_READONLY_CONFIG_KEYS:
            logger.warning("jsonrpc: config.set rejecting sensitive field %r (RPC read-only)", backend_key)
            return {
                "ok": False,
                "error": f"field {key!r} is security-sensitive and cannot be modified over RPC",
            }
        probe, warnings = EcosystemConfig.from_dict({backend_key: value})
        if warnings:
            logger.warning("jsonrpc: config.set %s validation failed: %s", key, "; ".join(warnings))
            return {"ok": False, "error": "; ".join(warnings)}
        setattr(self.config, backend_key, getattr(probe, backend_key))
        self.config._notify_change(backend_key, None, getattr(probe, backend_key))
        # A4: config changes persist to config_center, restorable after restart
        self._persist_config(backend_key, getattr(probe, backend_key))
        return {"ok": True}

    # plugins/states: return all plugin state snapshots (feeds the Studio
    # status panel).
    async def _plugins_states(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"states": self.lifecycle.list_states()}

    # plugins/state.get: return a single plugin state snapshot (dict envelope).
    async def _plugins_state_get(self, params: dict[str, Any]) -> dict[str, Any]:
        plugin_id = params.get("plugin_id", "")
        # P2-2: plugin_id format validation
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

    # plugins/state.list: return plugin snapshots filtered by state.
    async def _plugins_state_list(self, params: dict[str, Any]) -> dict[str, Any]:
        state_str = params.get("state", "")
        try:
            state_enum = PluginState(state_str)
        except ValueError:
            return {"plugins": []}
        return {"plugins": self.lifecycle.list_by_state(state_enum)}

    # plugins/token.records: return token records (Studio field naming).
    async def _plugins_token_records(self, params: dict[str, Any]) -> dict[str, Any]:
        plugin_id = params.get("plugin_id")
        records = self.token_meter.records_for(plugin_id) if plugin_id else self.token_meter.all_records()
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

    # plugins/token.prune: prune old token records by age.
    # max_age_seconds has a floor of _PRUNE_MIN_AGE seconds, preventing an RPC
    # from wiping all audit records.
    async def _plugins_token_prune(self, params: dict[str, Any]) -> dict[str, Any]:
        max_age = params.get("max_age_seconds", 3600)
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = 3600.0
        max_age = max(max_age, _PRUNE_MIN_AGE)
        self.token_meter.prune(max_age_seconds=max_age)
        return {"ok": True}

    # plugins/vram.usage: return vRAM usage (Studio structure total/used/free/by_plugin).
    async def _plugins_vram_usage(self, params: dict[str, Any]) -> dict[str, Any]:
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

    # plugins/logs.stream: return the log buffer (Studio PluginLogEntry naming).
    # Studio requires id to be a String while the backend buffer id is an int,
    # so it is converted here.
    async def _plugins_logs_stream(self, params: dict[str, Any]) -> dict[str, Any]:
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

    # plugins/mcp.sessions: return the MCP session list (Studio MCPSession naming).
    async def _plugins_mcp_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
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

    async def _plugins_mcp_sessions_prune(self, params: dict[str, Any]) -> dict[str, Any]:
        # plugins/mcp.sessions.prune: prune expired MCP sessions by age.
        # max_age_seconds has a floor of _PRUNE_MIN_AGE seconds, preventing an
        # RPC from wiping everything.
        max_age = params.get("max_age_seconds", 3600)
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = 3600.0
        max_age = max(max_age, _PRUNE_MIN_AGE)
        self.prune_sessions(max_age_seconds=max_age)
        return {"ok": True}

    # ── Internal helpers ──

    # Project the backend EcosystemConfig into Studio's expected 7 field names.
    def _config_to_studio_dict(self, config: EcosystemConfig) -> dict[str, Any]:
        backend = config.to_dict()
        result: dict[str, Any] = {}
        for studio_key, backend_key in _STUDIO_CONFIG_KEYS.items():
            result[studio_key] = backend.get(backend_key)
        return result

    # ── A4 persistence helpers: silently degrade when config_center is
    # missing (standalone/tests) ──

    _PERSIST_CONFIG_PREFIX = "plugin_ecosystem.config."
    _PERSIST_INSTALLED_KEY = "plugin_ecosystem.installed_ids"

    # Persist a single-field config change to the Desk config_center.
    def _persist_config(self, key: str, value: Any) -> None:
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            cc.set(self._PERSIST_CONFIG_PREFIX + key, value)
        except Exception as exc:
            logger.warning("jsonrpc: config persistence %s failed: %s", key, exc)

    # Persist the full config (called to flush before startup restore).
    def persist_full_config(self) -> None:
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            for key, value in self.config.to_dict().items():
                cc.set(self._PERSIST_CONFIG_PREFIX + key, value)
        except Exception as exc:
            logger.warning("jsonrpc: full config persistence failed: %s", exc)

    # Update the persisted installed plugin ID set.
    def _persist_installed(self, plugin_id: str, installed: bool) -> None:
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return
        try:
            raw = cc.get(self._PERSIST_INSTALLED_KEY) or []
            ids = set(raw) if isinstance(raw, list) else set()
            ids.add(plugin_id) if installed else ids.discard(plugin_id)
            cc.set(self._PERSIST_INSTALLED_KEY, sorted(ids))
        except Exception as exc:
            logger.warning("jsonrpc: installed-state persistence %s failed: %s", plugin_id, exc)

    # Read the persisted installed plugin ID list from config_center (used by
    # server startup restore).
    def restore_installed(self) -> list[str]:
        cc = getattr(self.desk, "config_center", None)
        if cc is None:
            return []
        try:
            raw = cc.get(self._PERSIST_INSTALLED_KEY)
            return list(raw) if isinstance(raw, list) else []
        except Exception:
            return []

    # Read persisted config from config_center and merge into self.config,
    # returning the list of restored keys.
    def restore_config(self) -> list[str]:
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
            logger.warning("jsonrpc: config restore failed: %s", exc)
            return []
        if patch:
            merged, warnings = EcosystemConfig.from_dict({**self.config.to_dict(), **patch})
            self.config = merged
            if warnings:
                logger.warning("jsonrpc: restored config validation warnings: %s", "; ".join(warnings))
            # P2-1: restored config must be applied to lifecycle and
            # token_meter, otherwise at server startup both remain bound to the
            # old config and timeout/heartbeat/token persistence thresholds
            # silently stop working.
            self._apply_config_to_deps()
        return restored

    # Sync self.config into lifecycle and token_meter (call after config restore).
    # lifecycle reads config live via getters, so only its config reference
    # needs replacing; token_meter's max_records/persist_path are
    # construction-time snapshots needing explicit refresh.
    def _apply_config_to_deps(self) -> None:
        try:
            if self.lifecycle is not None:
                self.lifecycle.config = self.config
        except Exception as exc:
            logger.warning("jsonrpc: syncing config to lifecycle failed: %s", exc)
        try:
            if self.token_meter is not None:
                self.token_meter._max_records = self.config.max_token_records
                self.token_meter._persist_path = self.config.token_persist_path
        except Exception as exc:
            logger.warning("jsonrpc: syncing config to token_meter failed: %s", exc)

    # C12: session management
    def _touch_session(self, session_id: str, plugin_id: str) -> None:
        # sessionId format validation, rejecting forged unbounded IDs (P0-5)
        if not _SESSION_ID_RE.match(session_id):
            logger.warning("jsonrpc: rejecting invalid sessionId %r", session_id)
            return
        # R5: lock shared state so concurrent touches by multiple handlers do
        # not lose or corrupt sessions
        with self._state_lock:
            # Session cap + LRU eviction to prevent memory exhaustion (P0-5)
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
        self.desk.metrics.gauge("active_sessions", "current active MCP sessions").set(len(self._sessions))

    # Evict the least recently active session (LRU). Caller must hold _state_lock.
    def _evict_oldest_session(self) -> None:
        if not self._sessions:
            return
        oldest_id = min(self._sessions, key=lambda s: self._sessions[s]["last_active"])
        del self._sessions[oldest_id]
        logger.info("jsonrpc: session cap reached, LRU evicted %s", oldest_id)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [{"session_id": sid, **s} for sid, s in self._sessions.items()]

    def prune_sessions(self, max_age_seconds: float = 3600) -> int:
        with self._state_lock:
            cutoff = time.time() - max_age_seconds
            stale = [sid for sid, s in self._sessions.items() if s["last_active"] < cutoff]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)
        self.desk.metrics.gauge("active_sessions", "current active MCP sessions").set(len(self._sessions))

    # C13: rate limiting
    def _check_rate_limit(self, plugin_id: str) -> bool:
        now = time.time()
        cutoff = now - 60.0
        # R5: lock the shared timestamp table so concurrent counting by
        # multiple handlers neither under- nor over-counts
        with self._state_lock:
            timestamps = self._call_timestamps.get(plugin_id)
            if timestamps is None:
                timestamps = deque()
                self._call_timestamps[plugin_id] = timestamps
            # Evict expired timestamps from the left (enqueued in time order,
            # oldest on the left)
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._rate_limit:
                logger.warning(
                    "jsonrpc: rate limit triggered for plugin %s (%d/min)",
                    plugin_id,
                    self._rate_limit,
                )
                return False
            timestamps.append(now)
            return True

    # Convert a PluginManifest into an MCP Tool description (delegates to the
    # SSOT, avoiding a forked duplicate).
    def _manifest_to_mcp_tool(self, manifest: Any) -> dict[str, Any] | None:
        return manifest_to_mcp_tool(manifest)

    # Format a plugin return value into an MCP content array.
    def _format_result(self, result: Any) -> list[dict[str, Any]]:
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
