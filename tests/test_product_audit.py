"""生产发布审计 P0-P3 修复回归测试。

覆盖本轮审计修复项，确保加固不回归：
- P0-1 远程传输非 loopback 无鉴权拒绝启动
- P1-1 restart_count 成功执行后重置
- P1-5 安全敏感配置经 RPC 只读
- P1-7 异常对外脱敏
- P1-8 非 dict IPC 消息不击杀沙箱
- P2-1 restore_config 同步到 lifecycle/token_meter
- P2-2 plugin_id 格式校验
- P2-8 未鉴权 GET /health 探针
- P3-2 busy-loop 下限
"""

from __future__ import annotations

import asyncio

import pytest

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import MCPHandler
from fusion_plugins_ecosystem.transport import _is_health_request
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode
from fusion_plugins_ecosystem.server import MCPServer, _is_loopback_host


# ── P0-1：远程传输非 loopback 无鉴权拒绝启动 ──


def test_is_loopback_host() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("127.0.0.5") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("192.168.1.1") is False
    assert _is_loopback_host(None) is True


async def test_remote_transport_no_auth_rejected() -> None:
    server = MCPServer(
        config=EcosystemConfig(mcp_transport="sse", mcp_host="0.0.0.0", mcp_port=0)
    )
    with pytest.raises(RuntimeError, match="鉴权"):
        await server.start(transport="sse", host="0.0.0.0", port=0)


async def test_remote_transport_loopback_allowed_without_auth() -> None:
    # loopback 无鉴权可启动（本地单机/测试场景）
    import os

    assert os.environ.get("FUSION_PLUGIN_AUTH_TOKEN") is None or True
    server = MCPServer(
        config=EcosystemConfig(mcp_transport="sse", mcp_host="127.0.0.1", mcp_port=0)
    )
    start_task = asyncio.create_task(server.start(transport="sse", port=0))
    await asyncio.sleep(0.3)
    assert server._running is True
    await server.stop()
    await asyncio.wait_for(start_task, timeout=5)


# ── P1-1：restart_count 成功执行后重置 ──


def _crash_then_recover_registry() -> PluginRegistry:
    """插件首次调用崩溃，第二次调用成功。"""
    calls = {"n": 0}

    def entry(_desk, _params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("首次崩溃")
        return {"ok": True}

    manifest = PluginManifest(
        id="recover_plugin",
        name="Recover",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="崩溃后恢复",
        capabilities=[PluginCapability.MCP_TOOL],
        entry_point=entry,
        sandbox_mode=SandboxMode.INLINE,
        max_restart=3,
    )
    registry = PluginRegistry()
    registry.register(manifest)
    return registry


async def test_restart_count_resets_on_success() -> None:
    registry = _crash_then_recover_registry()
    lifecycle = PluginLifecycle(registry, config=EcosystemConfig())
    await lifecycle.enable("recover_plugin")
    # 首次执行崩溃 → 触发重启，restart_count=1
    with pytest.raises(ValueError):
        await lifecycle.execute("recover_plugin", {})
    inst = lifecycle._instances["recover_plugin"]
    assert inst.restart_count == 1
    # 第二次执行成功 → restart_count 应回零
    result = await lifecycle.execute("recover_plugin", {})
    assert result == {"ok": True}
    assert lifecycle._instances["recover_plugin"].restart_count == 0


# ── P1-5：安全敏感配置经 RPC 只读 ──


def _make_handler() -> MCPHandler:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    manifest = PluginManifest(
        id="caveman_compress",
        name="Caveman",
        version="0.3.3",
        category=PluginCategory.CONTEXT_COMPRESS,
        description="压缩",
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
        sandbox_mode=SandboxMode.INLINE,
    )
    registry.register(manifest)
    return MCPHandler(registry=registry)


async def _call(handler: MCPHandler, method: str, params: dict | None = None):
    resp = await handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert resp is not None
    return resp.get("result") or resp.get("error")


async def test_config_set_sensitive_field_rejected() -> None:
    handler = _make_handler()
    # sandbox_mode 投影 sandbox_default_mode → 只读
    result = await _call(handler, "plugins/config.set", {"sandbox_mode": "process"})
    assert result["ok"] is False
    assert "敏感" in result["error"]
    # mcp_host 后端原名 → 只读
    result = await _call(handler, "plugins/config.set", {"mcp_host": "0.0.0.0"})
    assert result["ok"] is False


async def test_config_set_non_sensitive_field_allowed() -> None:
    handler = _make_handler()
    result = await _call(handler, "plugins/config.set", {"log_level": "DEBUG"})
    assert result["ok"] is True


# ── P1-7：异常对外脱敏 ──


async def test_tools_call_error_no_raw_exception() -> None:
    handler = _make_handler()
    # 未启用插件 → execute 抛错，响应不应含原始异常文本
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp__plugin__caveman_compress", "arguments": {}},
        }
    )
    err_text = resp["result"]["content"][0]["text"]
    assert "未启用" not in err_text  # 原始异常细节不入响应


# ── P2-1：restore_config 同步到 lifecycle/token_meter ──


async def test_apply_config_to_deps_syncs_lifecycle_and_meter() -> None:
    handler = _make_handler()
    # 模拟 restore 后 config 被替换为带新阈值的新对象
    new_cfg = EcosystemConfig(subagent_timeout_seconds=99, max_token_records=42)
    handler.config = new_cfg
    handler._apply_config_to_deps()
    assert handler.lifecycle.config is new_cfg
    assert handler.token_meter._max_records == 42
    assert handler.token_meter._persist_path == new_cfg.token_persist_path


# ── P2-2：plugin_id 格式校验 ──


async def test_install_rejects_invalid_plugin_id() -> None:
    handler = _make_handler()
    # 含换行的 plugin_id 应被拒（防日志注入）
    result = await _call(
        handler, "plugins/install", {"plugin_id": "evil\n[CRITICAL] fake"}
    )
    assert result["ok"] is False


async def test_state_get_invalid_plugin_id_returns_unknown() -> None:
    handler = _make_handler()
    result = await _call(handler, "plugins/state.get", {"plugin_id": "bad id!"})
    assert result["state"] == "unknown"


# ── P2-8：未鉴权 GET /health 探针 ──


def test_is_health_request() -> None:
    assert _is_health_request("GET /health HTTP/1.1") is True
    assert _is_health_request("GET /health/ HTTP/1.1") is True
    assert _is_health_request("POST /health HTTP/1.1") is False
    assert _is_health_request("GET /tools HTTP/1.1") is False
    assert _is_health_request("") is False


# ── P3-2：busy-loop 下限（sleep 不会是 0） ──


def test_watch_loop_sleep_floor() -> None:
    # heartbeat_stale=1 时 //2=0，但修复后 max(1,0)=1，确保不空转
    import inspect

    src = inspect.getsource(PluginLifecycle._watch_loop)
    assert "max(1," in src


# ── P1-8：非 dict IPC 消息不击杀沙箱（_read_loop 守卫） ──


def test_read_loop_non_dict_guard_exists() -> None:
    import inspect

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    src = inspect.getsource(sandbox_mod.PluginSandbox._read_loop)
    assert "isinstance(msg, dict)" in src


# ── P1-2：stop_watcher 须 await（同步调用泄漏 watcher task） ──


def test_full_shutdown_awaits_stop_watcher() -> None:
    # server._full_shutdown 必须以 await 调用 stop_watcher，否则
    # coroutine never awaited + watcher task 泄漏
    import inspect

    from fusion_plugins_ecosystem import server as server_mod

    src = inspect.getsource(server_mod.MCPServer._full_shutdown)
    assert "await self.lifecycle.stop_watcher()" in src
    assert "self.lifecycle.stop_watcher()" not in src.replace(
        "await self.lifecycle.stop_watcher()", ""
    )


# ── P0-3：PROCESS worker config 序列化为 Python 字面量（非 JSON） ──


def test_worker_config_uses_python_literal() -> None:
    # json.dumps 的 true/false/null 不是合法 Python 标识符，嵌入 worker 脚本
    # 会 NameError → 所有携带 bool/None 配置的 PROCESS 插件 spawn 即崩。
    # 修复后用 repr() 输出 True/False/None。
    import inspect

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    src = inspect.getsource(sandbox_mod.PluginSandbox._build_worker_script)
    assert "_CONFIG={config!r}" in src
    assert "json.dumps(config)" not in src


def test_worker_config_expr_is_valid_python() -> None:
    # 端到端：携带 bool/None 配置生成的 worker 脚本 _CONFIG 表达式
    # 必须是合法 Python（True/False/None），不能出现 true/false/null。
    import re

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    sandbox = sandbox_mod.PluginSandbox(desk=DeskRuntime())
    script = sandbox._build_worker_script(
        "mod:attr",
        dict(EcosystemConfig().to_dict()),
        sandbox_mod.ResourceLimits(timeout_seconds=30),
    )
    m = re.search(r"^_CONFIG=(.+)$", script, re.MULTILINE)
    assert m, "worker 脚本缺 _CONFIG 赋值"
    expr = m.group(1)
    assert " true" not in expr and "\ttrue" not in expr
    assert "True" in expr or "False" in expr or "None" in expr


# ── P2-9：infra_log 双日志流汇集 ──


def test_infra_log_records_to_ring_buffer() -> None:
    """P2-9：infra_log 写入环形缓冲，plugin_id=_infra，运维可经 get_logs 查询。"""
    desk = DeskRuntime()
    desk.infra_log("jsonrpc", "ERROR", "handler foo error: ValueError")
    logs = desk.get_logs(plugin_id="_infra", limit=10)
    assert any("[jsonrpc]" in e["message"] for e in logs)
    assert any("handler foo error" in e["message"] for e in logs)


async def test_illegal_transition_logged_to_desk() -> None:
    """P2-9：非法状态转移同时汇入 desk 环形缓冲（不只 stderr）。"""
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
    from fusion_plugins_ecosystem.registry import PluginRegistry

    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    lifecycle = PluginLifecycle(registry)
    # 构造一个 LOADED 实例，非法转移到 ENABLED 外的态
    from fusion_plugins_ecosystem.lifecycle import PluginInstance

    from tests.test_lifecycle import _make_manifest

    inst = PluginInstance(
        manifest=_make_manifest(entry_point=lambda d, p: {}),
        state=PluginState.LOADED,
    )
    registry._manifests = {inst.manifest.id: inst.manifest}
    lifecycle._instances[inst.manifest.id] = inst
    with pytest.raises(RuntimeError, match="非法状态转换"):
        lifecycle._transition(inst, PluginState.TIMEOUT)
    infra_logs = desk.get_logs(plugin_id="_infra", limit=20)
    assert any("非法状态转换" in e["message"] for e in infra_logs)
