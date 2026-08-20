"""Studio 集成面板 plugins/* JSON-RPC 方法测试。

校验 dict 信封 + 具名键严格匹配 PluginBridge.swift / PluginEcosystemModels.swift
的 fromDict 约定。覆盖全部 15 个方法。
"""

from __future__ import annotations

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import MCPHandler
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenRecord


def _registry_with_plugin() -> tuple[PluginRegistry, PluginManifest]:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    manifest = PluginManifest(
        id="caveman_compress",
        name="Caveman 压缩",
        version="0.3.3",
        category=PluginCategory.CONTEXT_COMPRESS,
        description="上下文压缩插件",
        capabilities=(PluginCapability.MCP_TOOL,),
        params=(
            PluginParam(
                name="ratio",
                type=PluginParamType.INT,
                description="压缩比",
                required=False,
                default=2,
            ),
        ),
        default_mounted=True,
        vram_mb=0,
        sandbox_mode=SandboxMode.INLINE,
    )
    registry.register(manifest)
    return registry, manifest


def _make_handler(
    registry: PluginRegistry | None = None,
) -> tuple[MCPHandler, PluginRegistry]:
    registry = registry or _registry_with_plugin()[0]
    handler = MCPHandler(registry=registry)
    return handler, registry


async def _call(handler: MCPHandler, method: str, params: dict | None = None):
    """通过 handle() 发起完整 JSON-RPC 请求，返回 result dict。"""
    resp = await handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert resp is not None
    assert "error" not in resp, f"unexpected error: {resp.get('error')}"
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    return resp["result"]


# ── plugins.ping ──


async def test_plugins_ping() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins.ping")
    assert result == {"pong": True}


# ── plugins/list ──


async def test_plugins_list_keys() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/list")
    assert "plugins" in result
    items = result["plugins"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]
    # Studio PluginListItem.fromDict 必需键
    for key in (
        "id",
        "name",
        "category",
        "version",
        "description",
        "author",
        "enabled",
        "installed",
    ):
        assert key in item, f"missing key {key}"
    assert item["id"] == "caveman_compress"
    assert item["name"] == "Caveman 压缩"
    assert item["category"] == "context_compress"
    assert item["version"] == "0.3.3"
    assert item["author"] is None
    assert item["enabled"] is False  # 未启用
    assert item["installed"] is False  # 未加载


async def test_plugins_list_with_category_filter() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/list", {"category": "custom"})
    assert result["plugins"] == []
    result = await _call(handler, "plugins/list", {"category": "context_compress"})
    assert len(result["plugins"]) == 1


# ── plugins/install + plugins/uninstall ──


async def test_plugins_install_uninstall() -> None:
    handler, registry = _make_handler()
    # install → 加载并启用
    result = await _call(handler, "plugins/install", {"plugin_id": "caveman_compress"})
    assert result == {"ok": True}
    # install 后状态应为 enabled
    state = await _call(handler, "plugins/state.get", {"plugin_id": "caveman_compress"})
    assert state["state"] == "enabled"
    assert state["error_count"] == 0
    # uninstall
    result = await _call(
        handler, "plugins/uninstall", {"plugin_id": "caveman_compress"}
    )
    assert result == {"ok": True}
    state = await _call(handler, "plugins/state.get", {"plugin_id": "caveman_compress"})
    assert state["state"] in ("disabled", "unknown")


async def test_plugins_install_unknown_plugin() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/install", {"plugin_id": "nope"})
    assert result["ok"] is False
    assert "error" in result


# ── plugins/config.get + config.set ──


async def test_plugins_config_get_studio_keys() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/config.get")
    # Studio EcosystemConfig.fromDict 期望 7 键
    for key in (
        "sandbox_mode",
        "auto_update",
        "max_concurrent_plugins",
        "log_level",
        "token_budget",
        "vram_limit_mb",
        "mcp_enabled",
    ):
        assert key in result, f"missing config key {key}"
    assert result["mcp_enabled"] is True  # enable_claude_mcp 默认 True
    assert result["sandbox_mode"] == "inline"


async def test_plugins_config_set_studio_name() -> None:
    handler, _ = _make_handler()
    # Studio 名 mcp_enabled → 后端 enable_claude_mcp
    result = await _call(handler, "plugins/config.set", {"mcp_enabled": False})
    assert result == {"ok": True}
    config = await _call(handler, "plugins/config.get")
    assert config["mcp_enabled"] is False


async def test_plugins_config_set_backend_name() -> None:
    handler, _ = _make_handler()
    # 后端原名也支持
    result = await _call(handler, "plugins/config.set", {"enable_claude_mcp": True})
    assert result == {"ok": True}


# ── plugins/states + state.get + state.list ──


async def test_plugins_states_empty() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/states")
    assert result == {"states": []}


async def test_plugins_states_after_install() -> None:
    handler, _ = _make_handler()
    await _call(handler, "plugins/install", {"plugin_id": "caveman_compress"})
    result = await _call(handler, "plugins/states")
    states = result["states"]
    assert len(states) == 1
    # Studio PluginStateInfo.fromDict 必需键
    for key in (
        "id",
        "plugin_id",
        "state",
        "pid",
        "start_time",
        "uptime",
        "error_count",
        "last_error",
    ):
        assert key in states[0], f"missing state key {key}"
    assert states[0]["state"] == "enabled"
    assert isinstance(states[0]["start_time"], str)
    assert isinstance(states[0]["uptime"], int)


async def test_plugins_state_get_unknown() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/state.get", {"plugin_id": "ghost"})
    assert result["state"] == "unknown"
    assert result["id"] == "ghost"


async def test_plugins_state_list_by_state() -> None:
    handler, _ = _make_handler()
    await _call(handler, "plugins/install", {"plugin_id": "caveman_compress"})
    result = await _call(handler, "plugins/state.list", {"state": "enabled"})
    assert len(result["plugins"]) == 1
    result = await _call(handler, "plugins/state.list", {"state": "disabled"})
    assert result["plugins"] == []


async def test_plugins_state_list_invalid_state() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/state.list", {"state": "bogus"})
    assert result == {"plugins": []}


# ── plugins/token.records + token.prune ──


async def test_plugins_token_records_keys() -> None:
    handler, registry = _make_handler()
    # 注入一条记录
    handler.token_meter.record(
        TokenRecord(
            plugin_id="caveman_compress",
            kind=TokenKind.CLAUDE_MODEL,
            input_tokens=100,
            output_tokens=50,
            metadata={"model": "claude-fable-5"},
        )
    )
    result = await _call(handler, "plugins/token.records")
    assert "records" in result
    recs = result["records"]
    assert len(recs) == 1
    # Studio TokenRecord.fromDict 必需键
    for key in (
        "id",
        "plugin_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "timestamp",
        "model",
    ):
        assert key in recs[0], f"missing token key {key}"
    assert recs[0]["prompt_tokens"] == 100
    assert recs[0]["completion_tokens"] == 50
    assert recs[0]["total_tokens"] == 150
    assert recs[0]["model"] == "claude-fable-5"
    assert isinstance(recs[0]["timestamp"], str)


async def test_plugins_token_records_by_plugin() -> None:
    handler, registry = _make_handler()
    handler.token_meter.record(
        TokenRecord(plugin_id="caveman_compress", kind=TokenKind.PLUGIN_LOCAL)
    )
    handler.token_meter.record(
        TokenRecord(plugin_id="other", kind=TokenKind.PLUGIN_LOCAL)
    )
    result = await _call(
        handler, "plugins/token.records", {"plugin_id": "caveman_compress"}
    )
    recs = result["records"]
    assert len(recs) == 1
    assert recs[0]["plugin_id"] == "caveman_compress"


async def test_plugins_token_prune() -> None:
    handler, registry = _make_handler()
    handler.token_meter.record(
        TokenRecord(plugin_id="caveman_compress", kind=TokenKind.PLUGIN_LOCAL)
    )
    result = await _call(handler, "plugins/token.prune", {"max_age_seconds": 0})
    assert result == {"ok": True}
    # max_age 被 floor 到 60s，刚写入记录 timestamp > now-60 → 保留，防止审计全清
    recs = await _call(handler, "plugins/token.records")
    assert len(recs["records"]) == 1


async def test_plugins_token_prune_floor_prevents_wipe() -> None:
    handler, registry = _make_handler()
    handler.token_meter.record(
        TokenRecord(plugin_id="caveman_compress", kind=TokenKind.PLUGIN_LOCAL)
    )
    # 负数 / 0 都应被 floor，记录不被清空
    await _call(handler, "plugins/token.prune", {"max_age_seconds": -1})
    recs = await _call(handler, "plugins/token.records")
    assert len(recs["records"]) == 1


# ── plugins/vram.usage ──


async def test_plugins_vram_usage_keys() -> None:
    handler, registry = _make_handler()
    # 注入显存分配
    registry.desk.acquire_vram("caveman_compress", 256)
    registry.desk.vram_total_mb = 4096
    result = await _call(handler, "plugins/vram.usage")
    # Studio VRAMUsage.fromDict 必需键
    for key in ("total_mb", "used_mb", "free_mb", "by_plugin"):
        assert key in result, f"missing vram key {key}"
    assert result["total_mb"] == 4096
    assert result["used_mb"] == 256
    assert result["free_mb"] == 3840
    entries = result["by_plugin"]
    assert len(entries) == 1
    entry = entries[0]
    for key in ("id", "plugin_id", "allocated_mb", "peak_mb"):
        assert key in entry, f"missing vram entry key {key}"
    assert entry["allocated_mb"] == 256
    assert entry["plugin_id"] == "caveman_compress"


async def test_plugins_vram_usage_unlimited() -> None:
    handler, registry = _make_handler()
    registry.desk.acquire_vram("caveman_compress", 100)
    result = await _call(handler, "plugins/vram.usage")
    assert result["total_mb"] == 0
    assert result["used_mb"] == 100
    assert result["free_mb"] == 0  # total=0 → free=0


# ── plugins/logs.stream ──


async def test_plugins_logs_stream_keys() -> None:
    handler, registry = _make_handler()
    registry.desk.log("caveman_compress", "INFO", "插件已加载")
    registry.desk.log("caveman_compress", "ERROR", "执行失败")
    result = await _call(handler, "plugins/logs.stream")
    assert "entries" in result
    entries = result["entries"]
    assert len(entries) == 2
    # Studio PluginLogEntry.fromDict 必需键（id 必须为 String）
    for key in ("id", "plugin_id", "level", "message", "timestamp"):
        assert key in entries[0], f"missing log key {key}"
    assert isinstance(entries[0]["id"], str)
    assert entries[0]["plugin_id"] == "caveman_compress"
    assert entries[1]["level"] == "ERROR"


async def test_plugins_logs_stream_filter() -> None:
    handler, registry = _make_handler()
    registry.desk.log("caveman_compress", "INFO", "ok")
    registry.desk.log("other", "ERROR", "bad")
    result = await _call(
        handler, "plugins/logs.stream", {"plugin_id": "caveman_compress"}
    )
    entries = result["entries"]
    assert len(entries) == 1
    assert entries[0]["plugin_id"] == "caveman_compress"
    result = await _call(handler, "plugins/logs.stream", {"level": "ERROR"})
    entries = result["entries"]
    assert all(e["level"] == "ERROR" for e in entries)


# ── plugins/mcp.sessions + mcp.sessions.prune ──


async def test_plugins_mcp_sessions_keys() -> None:
    handler, registry = _make_handler()
    # 触发一次会话记录
    handler._touch_session("sess-1", "caveman_compress")
    result = await _call(handler, "plugins/mcp.sessions")
    assert "sessions" in result
    sessions = result["sessions"]
    assert len(sessions) == 1
    # Studio MCPSession.fromDict 必需键
    for key in (
        "id",
        "session_id",
        "plugin_id",
        "server",
        "status",
        "tool_count",
        "connected_at",
    ):
        assert key in sessions[0], f"missing session key {key}"
    assert sessions[0]["id"] == "sess-1"
    assert sessions[0]["session_id"] == "sess-1"
    assert sessions[0]["server"] == "fusion-plugins-ecosystem"
    assert isinstance(sessions[0]["connected_at"], str)


async def test_plugins_mcp_sessions_prune() -> None:
    import time as _time

    handler, registry = _make_handler()
    handler._touch_session("sess-old", "caveman_compress")
    # 手动使会话 last_active 老于 floor（60s），确保被淘汰
    handler._sessions["sess-old"]["last_active"] = _time.time() - 120
    result = await _call(handler, "plugins/mcp.sessions.prune", {"max_age_seconds": 60})
    assert result == {"ok": True}
    sessions = await _call(handler, "plugins/mcp.sessions")
    assert sessions["sessions"] == []


# ── 完整信封校验：错误方法名 ──


async def test_unknown_plugins_method_returns_error() -> None:
    handler, _ = _make_handler()
    resp = await handler.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "plugins/nonexistent", "params": {}}
    )
    assert resp is not None
    assert resp["error"]["code"] == -32601
    assert resp["id"] == 9


# ── 安全加固：config.set 校验 / prune floor / last_error 截断 ──


async def test_plugins_config_set_rejects_bad_value() -> None:
    handler, _ = _make_handler()
    # mcp_port 越界（>65535）→ 校验器拒绝，返回 ok:False
    result = await _call(handler, "plugins/config.set", {"mcp_port": 99999})
    assert result["ok"] is False
    assert "error" in result
    # 配置未被污染
    config = await _call(handler, "plugins/config.get")
    assert config["vram_limit_mb"] == 0  # mcp_port 投影名


async def test_plugins_config_set_rejects_unknown_key() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/config.set", {"nope_field": 1})
    assert result["ok"] is False
    assert "error" in result


async def test_plugins_config_set_rejects_empty() -> None:
    handler, _ = _make_handler()
    result = await _call(handler, "plugins/config.set", {})
    assert result["ok"] is False


async def test_plugins_mcp_sessions_prune_floor() -> None:
    handler, registry = _make_handler()
    handler._touch_session("sess-x", "caveman_compress")
    # 0 被 floor，刚 touch 的会话 last_active > now-60 → 保留
    result = await _call(handler, "plugins/mcp.sessions.prune", {"max_age_seconds": 0})
    assert result == {"ok": True}
    sessions = await _call(handler, "plugins/mcp.sessions")
    assert len(sessions["sessions"]) == 1


async def test_plugins_state_last_error_truncated() -> None:
    handler, registry = _make_handler()
    await _call(handler, "plugins/install", {"plugin_id": "caveman_compress"})
    # 直接注入超长 last_error 模拟泄露路径
    inst = handler.lifecycle._instances["caveman_compress"]
    inst.last_error = "x" * 5000
    state = await _call(handler, "plugins/state.get", {"plugin_id": "caveman_compress"})
    err = state["last_error"]
    assert err is not None
    assert len(err) <= 303  # 300 + 省略号
    assert err.endswith("…")
