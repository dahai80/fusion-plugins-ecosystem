"""插件生命周期管理器测试。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import (
    PluginInstance,
    PluginLifecycle,
    PluginState,
)
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import SandboxMode


# ── 测试用插件工厂 ──


def _make_manifest(
    plugin_id: str = "test_plugin",
    vram_mb: int = 0,
    timeout_seconds: int | None = None,
    entry_point: Any = None,
    sandbox_mode: SandboxMode = SandboxMode.INLINE,
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="测试插件",
        capabilities=[PluginCapability.CLAUDE_SKILL],
        entry_point=entry_point,
        vram_mb=vram_mb,
        timeout_seconds=timeout_seconds,
        sandbox_mode=sandbox_mode,
    )


def _make_registry(*manifests: PluginManifest) -> PluginRegistry:
    registry = PluginRegistry()
    for m in manifests:
        registry.register(m)
    return registry


# ── load ──


def test_load_unknown_plugin_raises_keyerror() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    with pytest.raises(KeyError, match="未注册"):
        lifecycle.load("nonexistent")


def test_load_already_loaded_returns_existing() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    inst1 = lifecycle.load("test_plugin")
    inst2 = lifecycle.load("test_plugin")
    assert inst1 is inst2
    assert inst1.state == PluginState.LOADED


def test_load_string_entry_point_resolves() -> None:
    m = _make_manifest(
        entry_point="fusion_plugins_ecosystem.builtin.caveman_compress:caveman_compress"
    )
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    inst = lifecycle.load("test_plugin")
    assert inst.state == PluginState.LOADED


# ── enable / disable ──


async def test_enable_no_vram_succeeds() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    inst = await lifecycle.enable("test_plugin")
    assert inst.state == PluginState.ENABLED


async def test_enable_with_vram_succeeds() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=100)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    inst = await lifecycle.enable("test_plugin")
    assert inst.state == PluginState.ENABLED
    assert lifecycle.desk.vram_usage() == {"test_plugin": 100}


async def test_enable_vram_exceeds_budget_crashes() -> None:

    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=200)
    desk = DeskRuntime(vram_total_mb=100)
    registry = PluginRegistry(desk=desk)
    registry.register(m)
    lifecycle = PluginLifecycle(registry)
    # 显存超预算应抛 RuntimeError（P1-4），而非返回 CRASHED 实例误导调用方成功
    with pytest.raises(RuntimeError, match="显存申请失败"):
        await lifecycle.enable("test_plugin")
    inst = lifecycle._instances.get("test_plugin")
    assert inst is not None
    assert inst.state == PluginState.CRASHED


async def test_disable_releases_vram() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=100)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    await lifecycle.disable("test_plugin")
    assert lifecycle._instances["test_plugin"].state == PluginState.DISABLED
    assert lifecycle.desk.vram_usage() == {}


async def test_disable_unknown_plugin_noop() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    await lifecycle.disable("unknown")  # 不应抛异常


# ── unload ──


def test_unload_releases_vram() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=50)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("test_plugin")
    lifecycle.unload("test_plugin")
    assert "test_plugin" not in lifecycle._instances
    assert lifecycle.desk.vram_usage() == {}


def test_unload_unknown_plugin_noop() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    lifecycle.unload("unknown")  # 不应抛异常


# ── execute ──


async def test_execute_sync_entry_point() -> None:
    def entry(_desk, params):
        return {"echo": params["text"]}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    result = await lifecycle.execute("test_plugin", {"text": "hello"})
    assert result == {"echo": "hello"}


async def test_execute_idempotency_key_dedupes() -> None:
    """P2-5：携带 _idempotency_key 的重复请求命中缓存，跳过重复执行。"""
    call_count = {"n": 0}

    def entry(_desk, params):
        call_count["n"] += 1
        return {"seq": call_count["n"], "echo": params.get("text")}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")

    r1 = await lifecycle.execute("test_plugin", {"text": "a", "_idempotency_key": "k1"})
    r2 = await lifecycle.execute("test_plugin", {"text": "b", "_idempotency_key": "k1"})
    assert r1 == {"seq": 1, "echo": "a"}
    assert r2 == r1  # 命中缓存，不重复执行
    assert call_count["n"] == 1  # 入口仅执行一次


async def test_execute_idempotency_key_different_keys_independent() -> None:
    """P2-5：不同幂等键各自独立执行。"""
    call_count = {"n": 0}

    def entry(_desk, params):
        call_count["n"] += 1
        return {"seq": call_count["n"]}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")

    r1 = await lifecycle.execute("test_plugin", {"_idempotency_key": "k1"})
    r2 = await lifecycle.execute("test_plugin", {"_idempotency_key": "k2"})
    assert r1 == {"seq": 1}
    assert r2 == {"seq": 2}
    assert call_count["n"] == 2


async def test_execute_idempotency_key_not_passed_to_plugin() -> None:
    """P2-5：幂等键从 params 弹出，不透传给插件入口。"""
    seen = {}

    def entry(_desk, params):
        seen.update(params)
        return {"ok": True}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    await lifecycle.execute("test_plugin", {"text": "x", "_idempotency_key": "k1"})
    assert "_idempotency_key" not in seen
    assert seen.get("text") == "x"


async def test_execute_async_entry_point() -> None:
    async def entry(_desk, params):
        await asyncio.sleep(0.01)
        return {"async": params["text"]}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    result = await lifecycle.execute("test_plugin", {"text": "hi"})
    assert result == {"async": "hi"}


async def test_execute_not_enabled_raises() -> None:
    def entry(_desk, _params):
        return {}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("test_plugin")  # 加载但未启用
    with pytest.raises(RuntimeError, match="未启用"):
        await lifecycle.execute("test_plugin", {})


async def test_execute_unknown_plugin_raises() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    with pytest.raises(RuntimeError, match="未启用"):
        await lifecycle.execute("nonexistent", {})


async def test_execute_timeout_triggers_meltdown() -> None:
    async def entry(_desk, _params):
        await asyncio.sleep(10)
        return {}

    m = _make_manifest(entry_point=entry, timeout_seconds=0.1)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    with pytest.raises(asyncio.TimeoutError):
        await lifecycle.execute("test_plugin", {})
    inst = lifecycle._instances["test_plugin"]
    # 超时熔断后，_maybe_restart 把 state 重置为 ENABLED，restart_count 增加
    assert inst.restart_count >= 1


async def test_execute_crash_triggers_restart() -> None:
    call_count = 0

    def entry(_desk, _params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return {"recovered": True}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    await lifecycle.enable("test_plugin")
    with pytest.raises(RuntimeError, match="boom"):
        await lifecycle.execute("test_plugin", {})
    inst = lifecycle._instances["test_plugin"]
    # 崩溃后 _maybe_restart 重启，restart_count 增加
    assert inst.restart_count >= 1


async def test_execute_crash_sets_crashed_state() -> None:
    def entry(_desk, _params):
        raise ValueError("bad")

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.MAX_RESTART = 0  # 禁止重启，保留 CRASHED 状态
    await lifecycle.enable("test_plugin")
    with pytest.raises(ValueError, match="bad"):
        await lifecycle.execute("test_plugin", {})
    inst = lifecycle._instances["test_plugin"]
    assert inst.state == PluginState.CRASHED


# ── _maybe_restart ──


async def test_maybe_restart_respects_max_restart() -> None:
    def entry(_desk, _params):
        raise RuntimeError("always fail")

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.MAX_RESTART = 1
    await lifecycle.enable("test_plugin")
    with pytest.raises(RuntimeError):
        await lifecycle.execute("test_plugin", {})
    # 第二次崩溃，达到 MAX_RESTART，不再重启
    inst = lifecycle._instances["test_plugin"]
    inst.state = PluginState.ENABLED
    with pytest.raises(RuntimeError):
        await lifecycle.execute("test_plugin", {})


async def test_maybe_restart_unknown_plugin_noop() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    await lifecycle._maybe_restart("unknown")  # 不应抛异常


# ── _invoke ──


async def test_invoke_uncallable_entry_raises() -> None:
    m = _make_manifest(entry_point=None)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    # 直接构造一个 instance 为非 callable 的 PluginInstance
    inst = PluginInstance(manifest=m, state=PluginState.ENABLED, instance=12345)
    lifecycle._instances["test_plugin"] = inst
    with pytest.raises(RuntimeError, match="不可调用"):
        await lifecycle._invoke(inst, {})


# ── watcher ──


async def test_watcher_detects_stale_heartbeat() -> None:
    def entry(_desk, _params):
        return {}

    # watcher 仅对 PROCESS 沙箱生效（有独立心跳线程，能真实反映进程活性）；
    # inline 执行期间无法发心跳，watcher 介入会误杀合法长任务（P2-3）。
    m = _make_manifest(entry_point=entry, sandbox_mode=SandboxMode.PROCESS)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.HEARTBEAT_STALE = 0.05
    await lifecycle.enable("test_plugin")
    inst = lifecycle._instances["test_plugin"]
    # 模拟卡死：将心跳时间回退
    inst.last_heartbeat = time.time() - 100
    await lifecycle.start_watcher()
    await asyncio.sleep(0.15)
    await lifecycle.stop_watcher()
    assert inst.state == PluginState.TIMEOUT


async def test_start_watcher_idempotent() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    await lifecycle.start_watcher()
    task1 = lifecycle._watcher_task
    await lifecycle.start_watcher()
    assert lifecycle._watcher_task is task1
    await lifecycle.stop_watcher()


async def test_stop_watcher_when_none_noop() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    await lifecycle.stop_watcher()  # 不应抛异常


# ── PluginInstance dataclass ──


def test_plugin_instance_defaults() -> None:
    m = _make_manifest()
    inst = PluginInstance(manifest=m)
    assert inst.state == PluginState.REGISTERED
    assert inst.instance is None
    assert inst.restart_count == 0
    assert inst.last_token_record is None


def test_plugin_instance_to_dict() -> None:
    m = _make_manifest()
    inst = PluginInstance(manifest=m, state=PluginState.ENABLED, restart_count=2)
    d = inst.to_dict()
    assert d["plugin_id"] == "test_plugin"
    assert d["state"] == "enabled"
    assert d["restart_count"] == 2
    assert "last_heartbeat" in d


async def test_lifecycle_list_states() -> None:
    manifest = _make_manifest()
    registry = _make_registry(manifest)
    lifecycle = PluginLifecycle(registry)
    assert lifecycle.list_states() == []

    lifecycle.load("test_plugin")
    states = lifecycle.list_states()
    assert len(states) == 1
    assert states[0]["plugin_id"] == "test_plugin"
    assert states[0]["state"] == "loaded"

    await lifecycle.enable("test_plugin")
    states = lifecycle.list_states()
    assert states[0]["state"] == "enabled"


# ── 依赖自动加载 ──


def test_load_auto_loads_dependencies() -> None:
    dep = _make_manifest(plugin_id="dep_a", entry_point=lambda d, p: None)
    main_w_dep = PluginManifest(
        id="main_b",
        name="Main",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="depends on dep_a",
        capabilities=[PluginCapability.CLAUDE_SKILL],
        entry_point=lambda d, p: None,
        depends_on=("dep_a",),
    )
    registry = PluginRegistry()
    registry.register(dep)
    registry.register(main_w_dep)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("main_b")
    assert "dep_a" in lifecycle._instances
    assert "main_b" in lifecycle._instances


def test_load_circular_dependency_raises() -> None:
    a = PluginManifest(
        id="a",
        name="A",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=[],
        entry_point=lambda d, p: None,
        depends_on=("b",),
    )
    b = PluginManifest(
        id="b",
        name="B",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=[],
        entry_point=lambda d, p: None,
        depends_on=("a",),
    )
    registry = PluginRegistry()
    registry.register(a)
    registry.register(b)
    lifecycle = PluginLifecycle(registry)
    with pytest.raises(ValueError, match="循环依赖"):
        lifecycle.load("a")


def test_load_missing_dependency_raises() -> None:
    m = PluginManifest(
        id="orphan",
        name="Orphan",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=[],
        entry_point=lambda d, p: None,
        depends_on=("nonexistent",),
    )
    registry = PluginRegistry()
    registry.register(m)
    lifecycle = PluginLifecycle(registry)
    with pytest.raises(KeyError, match="未注册"):
        lifecycle.load("orphan")


async def test_per_plugin_max_restart_overrides_global() -> None:
    def entry(_desk, _params):
        raise RuntimeError("always fail")

    m = PluginManifest(
        id="limited",
        name="Limited",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=[PluginCapability.CLAUDE_SKILL],
        entry_point=entry,
        max_restart=0,
    )
    registry = PluginRegistry()
    registry.register(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.MAX_RESTART = 3
    await lifecycle.enable("limited")
    with pytest.raises(RuntimeError, match="always fail"):
        await lifecycle.execute("limited", {})
    inst = lifecycle._instances["limited"]
    assert inst.state == PluginState.CRASHED


async def test_per_plugin_timeout_overrides_global() -> None:
    async def entry(_desk, _params):
        await asyncio.sleep(10)
        return {}

    m = PluginManifest(
        id="quick_timeout",
        name="Quick",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=[PluginCapability.CLAUDE_SKILL],
        entry_point=entry,
        timeout_seconds=1,
    )
    registry = PluginRegistry()
    registry.register(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.DEFAULT_TIMEOUT = 600
    await lifecycle.enable("quick_timeout")
    with pytest.raises(asyncio.TimeoutError):
        await lifecycle.execute("quick_timeout", {})


# ── 状态查询 API ──


def test_get_state_returns_loaded() -> None:
    m = _make_manifest(entry_point=lambda d, p: None)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("test_plugin")
    state = lifecycle.get_state("test_plugin")
    assert state is not None
    assert state["plugin_id"] == "test_plugin"
    assert state["state"] == "loaded"


def test_get_state_returns_none_for_unknown() -> None:
    registry = _make_registry()
    lifecycle = PluginLifecycle(registry)
    assert lifecycle.get_state("no_such") is None


def test_list_by_state_filters() -> None:
    m1 = _make_manifest(plugin_id="a", entry_point=lambda d, p: None)
    m2 = _make_manifest(plugin_id="b", entry_point=lambda d, p: None)
    registry = PluginRegistry()
    registry.register(m1)
    registry.register(m2)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("a")
    lifecycle.load("b")
    loaded = lifecycle.list_by_state(PluginState.LOADED)
    assert len(loaded) == 2
    enabled = lifecycle.list_by_state(PluginState.ENABLED)
    assert len(enabled) == 0


# ── P3-6c: _enable_with_retry ──


async def test_enable_with_retry_succeeds_after_transient_vram() -> None:
    """显存申请失败这类暂态错误应重试，最终成功（不浪费崩溃重启预算）。"""

    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=100)
    desk = DeskRuntime(vram_total_mb=100)
    registry = PluginRegistry(desk=desk)
    registry.register(m)
    lifecycle = PluginLifecycle(registry)
    attempts = {"n": 0}

    real_acquire = desk.acquire_vram

    def flaky_acquire(plugin_id, mb):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("插件 'test_plugin' 显存申请失败，未启用")
        return real_acquire(plugin_id, mb)

    desk.acquire_vram = flaky_acquire
    inst = await lifecycle._enable_with_retry(
        "test_plugin", max_attempts=3, backoff=0.01
    )
    assert inst.state == PluginState.ENABLED
    assert attempts["n"] == 2  # 首次失败 + 重试一次成功


async def test_enable_with_retry_permanent_error_raises_immediately() -> None:
    """非显存争用的 RuntimeError（状态/配置错误）立即抛出，不重试。"""

    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry)
    registry = _make_registry(m)
    lifecycle = PluginLifecycle(registry)
    calls = {"n": 0}

    async def fail_once(plugin_id):
        calls["n"] += 1
        raise RuntimeError("状态转换非法")

    lifecycle.enable = fail_once
    with pytest.raises(RuntimeError, match="状态转换非法"):
        await lifecycle._enable_with_retry("test_plugin", max_attempts=3, backoff=0.01)
    assert calls["n"] == 1  # 未重试


async def test_enable_with_retry_exhausts_attempts_raises_last() -> None:
    """暂态错误重试耗尽后抛最后一次异常。"""

    def entry(_desk, _params):
        return {"ok": True}

    m = _make_manifest(entry_point=entry, vram_mb=100)
    desk = DeskRuntime(vram_total_mb=100)
    registry = PluginRegistry(desk=desk)
    registry.register(m)
    lifecycle = PluginLifecycle(registry)

    async def always_fail(plugin_id):
        raise RuntimeError("插件 'test_plugin' 显存申请失败，未启用")

    lifecycle.enable = always_fail
    with pytest.raises(RuntimeError, match="显存申请失败"):
        await lifecycle._enable_with_retry("test_plugin", max_attempts=3, backoff=0.01)
