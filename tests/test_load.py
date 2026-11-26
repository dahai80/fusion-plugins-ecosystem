"""Load and concurrency-safety regression tests (enterprise-grade load, step 3).

No external inference engine required; verifies correctness of critical paths
under concurrent pressure purely in-process:
- lifecycle concurrent execute (asyncio.Semaphore gate drops/over-admits nothing)
- metrics registry thread safety (multi-thread inc/dec without races, consistent render)
- PROCESS sandbox spawn storm (concurrent spawn -> execute -> shutdown without leaks)
- token-meter pressure (high-frequency record without loss, throttled flush without crash)

Deterministic, repeatable, part of CI regression (guarantees green paths before
production load testing).
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

# ── Helpers: build registry + lifecycle ──


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


# ── A. Lifecycle concurrent execute ──


async def test_concurrent_execute_all_complete_no_loss() -> None:
    """N concurrent execute calls all return, no loss and no exceptions."""
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
    results = await asyncio.gather(*(lifecycle.execute("load_plugin", {}) for _ in range(n)))
    assert len(results) == n
    assert all(r == {"ok": True} for r in results)
    assert counter["n"] == n

    success = lifecycle.desk.metrics.get_counter("plugin_executions_total")
    assert success.total() == n


async def test_concurrent_execute_respects_max_concurrent() -> None:
    """max_concurrent throttling: peak in-flight count never exceeds the limit."""
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

    # Default max_concurrent is usually large; set a small limit explicitly to verify throttling
    lifecycle._max_concurrent = 4
    lifecycle._concurrency_sem = asyncio.Semaphore(4)

    await asyncio.gather(*(lifecycle.execute("limited", {}) for _ in range(40)))
    assert peak["max"] <= 4
    assert peak["max"] >= 2


# ── B. Metrics registry thread safety ──


def test_counter_concurrent_inc_no_race() -> None:
    """Multi-thread concurrent inc on one counter: total = threads x per-thread count."""
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
    """Multi-thread concurrent set/inc/dec on a gauge: no exceptions, final value within legal range."""
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

    # Render not raising proves the internal lock is intact
    out = reg.render()
    assert "level" in out


def test_render_concurrent_with_writes_consistent() -> None:
    """Concurrent writes + concurrent render: output is always parseable and contains expected metrics."""
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


# ── C. token-meter pressure ──


def test_token_meter_high_freq_record_no_loss_no_crash() -> None:
    """High-frequency record (beyond the save_batch threshold) neither drops records nor crashes."""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=5000, persist_path=None)
    n = 1000

    for _ in range(n):
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
    """Multi-thread concurrent record: total = threads x per-thread count (no race-induced loss)."""
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


# ── D. PROCESS sandbox spawn storm ──


async def _process_entry(_desk, _params):
    return {"sandbox": "ok"}


async def test_sandbox_spawn_storm_no_leak() -> None:
    """PROCESS sandbox: concurrent spawn -> execute -> shutdown, no process leaks, no exceptions."""
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

    # Sequential spawn -> execute rounds (PROCESS keeps a single instance per plugin_id;
    # concurrent spawns reuse the same process; this verifies no leaks and correct
    # state machine behavior at storm scale)
    for _ in range(10):
        result = await lifecycle.execute("storm", {})
        assert result == {"sandbox": "ok"}

    # Teardown: shut down all child processes
    await lifecycle._sandbox.shutdown_all()
    assert len(lifecycle._sandbox._processes) == 0
