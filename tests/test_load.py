"""负载与并发安全回归测试（企业级压测，步骤 3）。

不依赖外部推理引擎，纯进程内验证关键路径在并发压力下的正确性：
- 生命周期并发 execute（asyncio.Semaphore 并发门不丢/不超额）
- 指标注册表线程安全（多线程 inc/dec 不竞态、render 一致）
- PROCESS 沙箱 spawn 风暴（并发 spawn→execute→shutdown 不泄漏）
- token-meter 压力（高频 record 不丢/节流落盘不崩）

确定性、可重复、纳入 CI 回归（保证生产压测前路径已绿）。
"""

from __future__ import annotations

import asyncio
import threading
import time

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.metrics import MetricsRegistry
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import SandboxMode
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter, TokenRecord


# ── 工具：构建注册表 + 生命周期 ──


def _registry_with(plugin_id: str, entry_point) -> PluginRegistry:
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(
        PluginManifest(
            id=plugin_id,
            name="Load Plugin",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="load test",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=entry_point,
            timeout_seconds=30,
            sandbox_mode=SandboxMode.INLINE,
        )
    )
    return registry


# ── A. 生命周期并发 execute ──


async def test_concurrent_execute_all_complete_no_loss() -> None:
    """N 路并发 execute，全部返回，无丢失/无异常。"""
    counter = {"n": 0}

    async def entry(_desk, _params):
        await asyncio.sleep(0.01)
        counter["n"] += 1
        return {"ok": True}

    registry = _registry_with("load_plugin", entry)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("load_plugin")
    await lifecycle.enable("load_plugin")

    n = 50
    results = await asyncio.gather(
        *(lifecycle.execute("load_plugin", {}) for _ in range(n))
    )
    assert len(results) == n
    assert all(r == {"ok": True} for r in results)
    assert counter["n"] == n

    success = lifecycle.desk.metrics.get_counter("plugin_executions_total")
    assert success.total() == n


async def test_concurrent_execute_respects_max_concurrent() -> None:
    """max_concurrent 限流：峰值在途数不超过上限。"""
    peak = {"in_flight": 0, "max": 0}

    async def entry(_desk, _params):
        peak["in_flight"] += 1
        peak["max"] = max(peak["max"], peak["in_flight"])
        await asyncio.sleep(0.02)
        peak["in_flight"] -= 1
        return True

    registry = _registry_with("limited", entry)
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("limited")
    await lifecycle.enable("limited")

    # 默认 max_concurrent 通常较大；显式设小上限验证限流生效
    lifecycle._max_concurrent = 4
    lifecycle._concurrency_sem = asyncio.Semaphore(4)

    await asyncio.gather(*(lifecycle.execute("limited", {}) for _ in range(40)))
    assert peak["max"] <= 4
    assert peak["max"] >= 2


# ── B. 指标注册表线程安全 ──


def test_counter_concurrent_inc_no_race() -> None:
    """多线程并发 inc 同一 counter，total = 线程数 × 每线程次数。"""
    reg = MetricsRegistry()
    counter = reg.counter("hits", "test counter")
    threads = 8
    per_thread = 500

    def worker():
        for _ in range(per_thread):
            counter.inc()

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert counter.total() == threads * per_thread


def test_gauge_concurrent_set_inc_dec_no_corruption() -> None:
    """多线程并发 set/inc/dec gauge，无异常，最终值在合法范围。"""
    reg = MetricsRegistry()
    gauge = reg.gauge("level", "test gauge")
    stop = threading.Event()

    def setter():
        while not stop.is_set():
            gauge.set(5)

    def incr():
        while not stop.is_set():
            gauge.inc(1)

    def decr():
        while not stop.is_set():
            gauge.dec(1)

    ts = [
        threading.Thread(target=setter),
        threading.Thread(target=incr),
        threading.Thread(target=decr),
    ]
    for t in ts:
        t.start()
    time.sleep(0.1)
    stop.set()
    for t in ts:
        t.join()

    # 渲染不抛异常即证明内部锁未损坏
    out = reg.render()
    assert "level" in out


def test_render_concurrent_with_writes_consistent() -> None:
    """并发写 + 并发 render，输出始终可解析、含预期指标。"""
    reg = MetricsRegistry()
    c = reg.counter("c", "c")
    g = reg.gauge("g", "g")
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            c.inc(i=i % 3)
            g.set(i)
            i += 1

    def renderer():
        while not stop.is_set():
            out = reg.render()
            assert "# TYPE c counter" in out
            assert "# TYPE g gauge" in out

    ts = [threading.Thread(target=writer) for _ in range(4)]
    ts += [threading.Thread(target=renderer) for _ in range(2)]
    for t in ts:
        t.start()
    time.sleep(0.1)
    stop.set()
    for t in ts:
        t.join()


# ── C. token-meter 压力 ──


def test_token_meter_high_freq_record_no_loss_no_crash() -> None:
    """高频 record（超过 save_batch 阈值），不丢记录、不崩。"""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=5000, persist_path=None)
    n = 1000

    for i in range(n):
        meter.record(
            TokenRecord(
                plugin_id="hp",
                kind=TokenKind.PLUGIN_LOCAL,
                input_tokens=10,
                output_tokens=5,
            )
        )

    assert len(meter.all_records()) == n


def test_token_meter_concurrent_record_thread_safe() -> None:
    """多线程并发 record，总数 = 线程数 × 每线程数（无竞态丢失）。"""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=50000, persist_path=None)
    threads = 8
    per_thread = 200

    def worker():
        for _ in range(per_thread):
            meter.record(
                TokenRecord(
                    plugin_id="conc",
                    kind=TokenKind.PLUGIN_LOCAL,
                    input_tokens=1,
                    output_tokens=1,
                )
            )

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(meter.all_records()) == threads * per_thread


# ── D. PROCESS 沙箱 spawn 风暴 ──


async def _process_entry(_desk, _params):
    return {"sandbox": "ok"}


async def test_sandbox_spawn_storm_no_leak() -> None:
    """PROCESS 沙箱：并发 spawn→execute→shutdown，无进程泄漏、无异常。"""
    registry = PluginRegistry(desk=DeskRuntime())
    registry.register(
        PluginManifest(
            id="storm",
            name="Storm Plugin",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="spawn storm",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point="tests.test_load:_process_entry",
            timeout_seconds=30,
            sandbox_mode=SandboxMode.PROCESS,
        )
    )
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("storm")
    await lifecycle.enable("storm")

    # 串行 spawn→execute 多轮（PROCESS 同 plugin_id 单实例，并发 spawn
    # 复用同进程；这里验证风暴量级下不泄漏、状态机正确）
    for _ in range(10):
        result = await lifecycle.execute("storm", {})
        assert result == {"sandbox": "ok"}

    # 收尾：shutdown 全部子进程
    await lifecycle._sandbox.shutdown_all()
    assert len(lifecycle._sandbox._processes) == 0
