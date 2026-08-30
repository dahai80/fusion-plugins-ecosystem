"""插件沙箱测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.sandbox import (
    PluginSandbox,
    ResourceLimits,
    SandboxHealth,
    SandboxProcess,
)
from fusion_plugins_ecosystem.schema import SandboxMode


# ── 模块级入口（PROCESS 沙箱通过 importlib 导入，不能用闭包/局部函数）──


def _sync_worker_entry(desk, params):
    desk.log("real_plugin", "INFO", "subprocess running", value=params.get("x"))
    return {"doubled": params.get("x", 0) * 2}


async def _async_worker_entry(desk, params):
    import asyncio

    await asyncio.sleep(0)
    return {"async_ok": params.get("y", "none")}


def _error_worker_entry(desk, params):
    raise ValueError("boom from worker")


def _exit_worker_entry(desk, params):
    import sys

    sys.exit(0)


# ── ResourceLimits ──


def test_resource_limits_defaults() -> None:
    limits = ResourceLimits()
    assert limits.memory_limit_mb == 512
    assert limits.cpu_limit == 1.0
    assert limits.timeout_seconds == 600
    assert limits.grace_period_seconds == 10
    assert limits.vram_budget_mb == 0


def test_resource_limits_custom() -> None:
    limits = ResourceLimits(
        memory_limit_mb=1024, timeout_seconds=300, vram_budget_mb=256
    )
    assert limits.memory_limit_mb == 1024
    assert limits.timeout_seconds == 300
    assert limits.vram_budget_mb == 256


def test_resource_limits_frozen() -> None:
    limits = ResourceLimits()
    with pytest.raises(AttributeError):
        limits.memory_limit_mb = 999


# ── SandboxHealth ──


def test_sandbox_health_values() -> None:
    assert SandboxHealth.ALIVE.value == "alive"
    assert SandboxHealth.DEAD.value == "dead"
    assert SandboxHealth.TIMEOUT.value == "timeout"
    assert SandboxHealth.KILLED.value == "killed"


# ── PluginSandbox init ──


def test_sandbox_init() -> None:
    sandbox = PluginSandbox()
    assert sandbox.default_limits.memory_limit_mb == 512
    assert len(sandbox._processes) == 0


def test_sandbox_init_with_custom_limits() -> None:
    limits = ResourceLimits(memory_limit_mb=2048)
    sandbox = PluginSandbox(default_limits=limits)
    assert sandbox.default_limits.memory_limit_mb == 2048


# ── PluginSandbox health ──


def test_sandbox_health_unknown_returns_dead() -> None:
    sandbox = PluginSandbox()
    assert sandbox.health("nonexistent") == SandboxHealth.DEAD


def test_sandbox_health_known_alive() -> None:
    sandbox = PluginSandbox()
    mock_proc = MagicMock()
    sandbox._processes["p1"] = SandboxProcess(
        plugin_id="p1",
        process=mock_proc,
        limits=ResourceLimits(),
    )
    assert sandbox.health("p1") == SandboxHealth.ALIVE


def test_sandbox_health_known_dead() -> None:
    sandbox = PluginSandbox()
    mock_proc = MagicMock()
    sp = SandboxProcess(plugin_id="p1", process=mock_proc, limits=ResourceLimits())
    sp.health = SandboxHealth.DEAD
    sandbox._processes["p1"] = sp
    assert sandbox.health("p1") == SandboxHealth.DEAD


# ── PluginSandbox kill ──


async def test_sandbox_kill_unknown_noop() -> None:
    sandbox = PluginSandbox()
    await sandbox.kill("nonexistent")


async def test_sandbox_kill_terminates_process() -> None:
    sandbox = PluginSandbox()
    mock_proc = MagicMock()
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = None
    sp = SandboxProcess(plugin_id="p1", process=mock_proc, limits=ResourceLimits())
    sandbox._processes["p1"] = sp

    await sandbox.kill("p1")
    mock_proc.terminate.assert_called_once()
    assert "p1" not in sandbox._processes


# ── PluginSandbox shutdown_all ──


async def test_sandbox_shutdown_all() -> None:
    sandbox = PluginSandbox()
    for pid in ("p1", "p2"):
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = None
        sp = SandboxProcess(plugin_id=pid, process=mock_proc, limits=ResourceLimits())
        sandbox._processes[pid] = sp

    await sandbox.shutdown_all()
    assert len(sandbox._processes) == 0


# ── PluginSandbox call ──


async def test_sandbox_call_unknown_plugin_raises() -> None:
    sandbox = PluginSandbox()
    with pytest.raises(KeyError, match="not spawned"):
        await sandbox.call("nonexistent", "execute", {})


async def test_sandbox_call_dead_plugin_raises() -> None:
    sandbox = PluginSandbox()
    mock_proc = MagicMock()
    sp = SandboxProcess(plugin_id="dead", process=mock_proc, limits=ResourceLimits())
    sp.health = SandboxHealth.DEAD
    sandbox._processes["dead"] = sp
    with pytest.raises(RuntimeError, match="dead"):
        await sandbox.call("dead", "execute", {})


# ── PluginSandbox spawn ──


async def test_sandbox_spawn_creates_process() -> None:
    sandbox = PluginSandbox()
    with patch(
        "fusion_plugins_ecosystem.sandbox.asyncio.create_subprocess_exec"
    ) as mock_exec:
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_exec.return_value = mock_process

        await sandbox.spawn("test_plugin", entry_point="my_module:my_func")
        mock_exec.assert_called_once()
        assert "test_plugin" in sandbox._processes
        assert sandbox.health("test_plugin") == SandboxHealth.ALIVE


async def test_sandbox_spawn_kills_old_first() -> None:
    sandbox = PluginSandbox()
    mock_proc = MagicMock()
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = None
    sp = SandboxProcess(plugin_id="p1", process=mock_proc, limits=ResourceLimits())
    sandbox._processes["p1"] = sp

    with patch(
        "fusion_plugins_ecosystem.sandbox.asyncio.create_subprocess_exec"
    ) as mock_exec:
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_exec.return_value = mock_process

        await sandbox.spawn("p1", entry_point="other:func")
        mock_proc.terminate.assert_called_once()


# ── _build_worker_script ──


def test_build_worker_script_string_entry() -> None:
    sandbox = PluginSandbox()
    script = sandbox._build_worker_script("mymod:myfunc", {"k": "v"}, ResourceLimits())
    assert "_ENTRY='mymod:myfunc'" in script
    # P0-3：config 经 repr 序列化为 Python 字面量（'k': 'v'），非 json 'k': 'v'
    assert "'k': 'v'" in script
    assert "_MEM_LIMIT_MB=512" in script


def test_build_worker_script_callable_entry() -> None:
    sandbox = PluginSandbox()

    def my_entry(config, params):
        return "ok"

    script = sandbox._build_worker_script(
        my_entry, {}, ResourceLimits(memory_limit_mb=1024)
    )
    assert "_ENTRY=" in script
    assert "_MEM_LIMIT_MB=1024" in script
    # _MEM_LIMIT_MB must be resolved at script generation time, not at runtime
    assert "_MEM_LIMIT_MB*1024*1024" in script


# ── Lifecycle INLINE mode ──


def _make_inline_manifest() -> PluginManifest:
    def entry(desk, params):
        return {"mode": "inline", "input": params.get("x", 0)}

    return PluginManifest(
        id="inline_plugin",
        name="Inline Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="inline mode plugin",
        capabilities=(PluginCapability.MCP_TOOL,),
        entry_point=entry,
        sandbox_mode=SandboxMode.INLINE,
    )


async def test_lifecycle_inline_execute() -> None:
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(_make_inline_manifest())
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("inline_plugin")
    result = await lifecycle.execute("inline_plugin", {"x": 42})
    assert result["mode"] == "inline"
    assert result["input"] == 42


# ── Lifecycle PROCESS mode (mocked sandbox) ──


def _make_process_manifest() -> PluginManifest:
    def entry(config, params):
        return {"mode": "process", "input": params.get("x", 0)}

    return PluginManifest(
        id="process_plugin",
        name="Process Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="process mode plugin",
        capabilities=(PluginCapability.MCP_TOOL,),
        entry_point=entry,
        sandbox_mode=SandboxMode.PROCESS,
    )


async def test_lifecycle_process_execute_with_mock() -> None:
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(_make_process_manifest())
    lifecycle = PluginLifecycle(registry)

    mock_sandbox = MagicMock(spec=PluginSandbox)
    mock_sandbox.health = MagicMock(return_value=SandboxHealth.DEAD)
    mock_sandbox.spawn = AsyncMock()
    mock_sandbox.call = AsyncMock(return_value={"mode": "process", "input": 99})
    mock_sandbox.kill = AsyncMock()
    lifecycle._sandbox = mock_sandbox

    await lifecycle.enable("process_plugin")
    result = await lifecycle.execute("process_plugin", {"x": 99})
    assert result["mode"] == "process"
    assert result["input"] == 99
    mock_sandbox.spawn.assert_called_once()
    mock_sandbox.call.assert_called_once()


async def test_lifecycle_process_reuses_sandbox_if_alive() -> None:
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(_make_process_manifest())
    lifecycle = PluginLifecycle(registry)

    mock_sandbox = MagicMock(spec=PluginSandbox)
    mock_sandbox.health = MagicMock(return_value=SandboxHealth.ALIVE)
    mock_sandbox.call = AsyncMock(return_value={"mode": "process", "input": 1})
    mock_sandbox.kill = AsyncMock()
    lifecycle._sandbox = mock_sandbox

    await lifecycle.enable("process_plugin")
    result = await lifecycle.execute("process_plugin", {"x": 1})
    assert result["input"] == 1
    mock_sandbox.spawn.assert_not_called()
    mock_sandbox.call.assert_called_once()


async def test_lifecycle_disable_kills_sandbox() -> None:
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(_make_process_manifest())
    lifecycle = PluginLifecycle(registry)

    mock_sandbox = MagicMock(spec=PluginSandbox)
    mock_sandbox.health = MagicMock(return_value=SandboxHealth.ALIVE)
    mock_sandbox.call = AsyncMock(return_value="ok")
    mock_sandbox.kill = AsyncMock()
    lifecycle._sandbox = mock_sandbox

    await lifecycle.enable("process_plugin")
    await lifecycle.execute("process_plugin", {})
    await lifecycle.disable("process_plugin")
    mock_sandbox.kill.assert_called_once_with("process_plugin")


# ── 真实子进程沙箱（验证 worker IPC 端到端）──


async def test_sandbox_real_subprocess_sync_entry() -> None:
    """真实子进程：同步入口 entry(desk, params) 签名，验证 desk 代理与结果回传。"""
    sandbox = PluginSandbox(default_limits=ResourceLimits(timeout_seconds=10))
    await sandbox.spawn("real_plugin", entry_point=_sync_worker_entry, config={})
    try:
        result = await sandbox.call("real_plugin", "execute", {"x": 21})
        assert result == {"doubled": 42}
    finally:
        await sandbox.kill("real_plugin")


async def test_sandbox_real_subprocess_async_entry() -> None:
    """真实子进程：异步入口 entry(desk, params) 签名，验证 asyncio.run 路径。"""
    sandbox = PluginSandbox(default_limits=ResourceLimits(timeout_seconds=10))
    await sandbox.spawn("async_plugin", entry_point=_async_worker_entry, config={})
    try:
        result = await sandbox.call("async_plugin", "execute", {"y": "yes"})
        assert result == {"async_ok": "yes"}
    finally:
        await sandbox.kill("async_plugin")


async def test_sandbox_real_subprocess_entry_error_propagates() -> None:
    """真实子进程：入口抛错时，宿主通过 error 消息收到异常。"""
    sandbox = PluginSandbox(default_limits=ResourceLimits(timeout_seconds=10))
    await sandbox.spawn("err_plugin", entry_point=_error_worker_entry, config={})
    try:
        with pytest.raises(RuntimeError, match="boom from worker"):
            await sandbox.call("err_plugin", "execute", {})
    finally:
        await sandbox.kill("err_plugin")


async def test_sandbox_real_subprocess_dead_resolves_pending() -> None:
    """真实子进程：进程退出后，等待中的调用应以异常结束而非永久挂起。"""
    sandbox = PluginSandbox(default_limits=ResourceLimits(timeout_seconds=10))
    await sandbox.spawn("exit_plugin", entry_point=_exit_worker_entry, config={})
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            await sandbox.call("exit_plugin", "execute", {})
    finally:
        await sandbox.kill("exit_plugin")
