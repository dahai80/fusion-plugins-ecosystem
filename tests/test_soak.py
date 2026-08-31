"""长时稳定性回归（企业级 soak，步骤 5）。

验证持续负载下无内存泄漏、无连接/会话累积、无指标漂移：
- token_meter 持续 record 超 max_records → 容量封顶不泄漏
- desk 日志环形缓冲持续 log → deque maxlen 封顶不泄漏
- MCPHandler 会话持续 _touch_session 新增 → _MAX_SESSIONS LRU 封顶不累积
- 指标计数器持续 inc → 单调无溢出/漂移
- lifecycle 持续 execute 500 次 → active_plugins 回基线无残留

确定性、入 CI、不依赖 live MLX。
"""

from __future__ import annotations

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime, _LOG_BUFFER_MAX
from fusion_plugins_ecosystem.jsonrpc import MCPHandler, _MAX_SESSIONS
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter, TokenRecord


def _registry() -> PluginRegistry:
    desk = DeskRuntime()
    reg = PluginRegistry(desk=desk)
    reg.register(
        PluginManifest(
            id="soak_p",
            name="Soak Plugin",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="soak stability",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=lambda d, p: {"ok": True},
            timeout_seconds=30,
        )
    )
    return reg


# ── token_meter 容量封顶 ──


def test_token_meter_cap_holds_under_sustained_record():
    """持续 record 远超 max_records → 容量封顶，不泄漏。"""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=500)

    for i in range(50_000):
        meter.record(
            TokenRecord(
                plugin_id="soak_p",
                kind=TokenKind.PLUGIN_LOCAL,
                input_tokens=10,
                output_tokens=5,
            )
        )

    assert len(meter.all_records()) == 500


# ── 日志环形缓冲封顶 ──


def test_log_buffer_cap_holds_under_sustained_log():
    """持续 log 远超 _LOG_BUFFER_MAX → deque 封顶，不泄漏。"""
    desk = DeskRuntime()
    for i in range(10_000):
        desk.log("soak_p", "INFO", f"msg {i}")
    assert len(desk.log_entries) == _LOG_BUFFER_MAX


# ── 会话 LRU 封顶 ──


def test_session_cap_holds_under_sustained_new_sessions():
    """持续 _touch_session 新增会话 → _MAX_SESSIONS LRU 封顶，不累积。"""
    reg = _registry()
    handler = MCPHandler(registry=reg)

    for i in range(2_000):
        handler._touch_session(f"sess-{i:05d}", "soak_p")

    assert len(handler._sessions) == _MAX_SESSIONS
    # 最新会话存活
    assert handler.get_session("sess-01999") is not None


def test_session_calls_list_bounded_under_sustained_touch():
    """同一会话持续 touch → calls 列表封顶（1000→500），不无限增长。"""
    reg = _registry()
    handler = MCPHandler(registry=reg)

    for i in range(5_000):
        handler._touch_session("sess-stable", "soak_p")

    session = handler.get_session("sess-stable")
    assert session is not None
    assert len(session["calls"]) <= 1000


# ── 指标计数器单调无漂移 ──


def test_counter_sustained_inc_monotonic_no_drift():
    """持续 inc → 总数精确，单调无溢出/漂移。"""
    desk = DeskRuntime()
    counter = desk.metrics.counter("soak_total", "soak total")

    for i in range(100_000):
        counter.inc()
    assert counter.total() == 100_000


def test_counter_labeled_sustained_inc_no_drift():
    """带标签分桶持续 inc → 各桶总数精确，总和精确。"""
    desk = DeskRuntime()
    counter = desk.metrics.counter("soak_labeled", "soak labeled")

    for i in range(10_000):
        counter.inc(plugin="p_a")
        counter.inc(plugin="p_b")
    assert counter.value(plugin="p_a") == 10_000
    assert counter.value(plugin="p_b") == 10_000
    assert counter.total() == 20_000


# ── lifecycle 持续 execute 无残留 ──


async def test_lifecycle_sustained_execute_no_residual():
    """500 次顺序 execute → 全成功，active_plugins 回基线无残留。"""
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle

    reg = _registry()
    lifecycle = PluginLifecycle(reg)
    lifecycle.load("soak_p")
    await lifecycle.enable("soak_p")

    active_gauge = reg.desk.metrics.gauge("active_plugins", "active")

    for i in range(500):
        await lifecycle.execute("soak_p", {})

    # 全部执行完成，活跃插件数稳定为 1（已启用，无并发残留）
    assert active_gauge.value() == 1


# ── token_meter 持续 record + 查询一致性 ──


def test_token_meter_sustained_record_query_consistent():
    """持续 record 后查询 → 总数/总数聚合一致，无丢失。"""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=10_000)

    for i in range(8_000):
        meter.record(
            TokenRecord(
                plugin_id="soak_p",
                kind=TokenKind.PLUGIN_LOCAL,
                input_tokens=3,
                output_tokens=2,
            )
        )

    records = meter.all_records()
    assert len(records) == 8_000
    total_in = sum(r.input_tokens for r in records)
    total_out = sum(r.output_tokens for r in records)
    assert total_in == 8_000 * 3
    assert total_out == 8_000 * 2
