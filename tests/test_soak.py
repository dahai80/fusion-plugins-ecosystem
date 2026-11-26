"""Long-run stability regression (enterprise soak, step 5).

Verifies no memory leaks, no connection/session accumulation, and no metric
 drift under sustained load:
- token_meter keeps recording past max_records -> capacity capped, no leak
- desk log ring buffer keeps logging -> deque maxlen capped, no leak
- MCPHandler keeps adding sessions via _touch_session -> _MAX_SESSIONS LRU capped, no accumulation
- metric counters keep inc -> monotonic, no overflow/drift
- lifecycle executes 500 times -> active_plugins back to baseline, no residue

Deterministic, CI-enabled, no live MLX required.
"""

from __future__ import annotations

from fusion_plugins_ecosystem.desk_runtime import _LOG_BUFFER_MAX, DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import _MAX_SESSIONS, MCPHandler
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


# ── token_meter capacity cap ──


def test_token_meter_cap_holds_under_sustained_record():
    """Sustained record far beyond max_records -> capacity capped, no leak."""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=500)

    for _i in range(50_000):
        meter.record(
            TokenRecord(
                plugin_id="soak_p",
                kind=TokenKind.PLUGIN_LOCAL,
                input_tokens=10,
                output_tokens=5,
            )
        )

    assert len(meter.all_records()) == 500


# ── Log ring buffer cap ──


def test_log_buffer_cap_holds_under_sustained_log():
    """Sustained log far beyond _LOG_BUFFER_MAX -> deque capped, no leak."""
    desk = DeskRuntime()
    for i in range(10_000):
        desk.log("soak_p", "INFO", f"msg {i}")
    assert len(desk.log_entries) == _LOG_BUFFER_MAX


# ── Session LRU cap ──


def test_session_cap_holds_under_sustained_new_sessions():
    """Sustained _touch_session additions -> _MAX_SESSIONS LRU capped, no accumulation."""
    reg = _registry()
    handler = MCPHandler(registry=reg)

    for i in range(2_000):
        handler._touch_session(f"sess-{i:05d}", "soak_p")

    assert len(handler._sessions) == _MAX_SESSIONS
    # Newest session still alive
    assert handler.get_session("sess-01999") is not None


def test_session_calls_list_bounded_under_sustained_touch():
    """Repeated touch of one session -> calls list capped (1000 -> 500), unbounded growth prevented."""
    reg = _registry()
    handler = MCPHandler(registry=reg)

    for _i in range(5_000):
        handler._touch_session("sess-stable", "soak_p")

    session = handler.get_session("sess-stable")
    assert session is not None
    assert len(session["calls"]) <= 1000


# ── Metric counter monotonic without drift ──


def test_counter_sustained_inc_monotonic_no_drift():
    """Sustained inc -> exact total, monotonic, no overflow/drift."""
    desk = DeskRuntime()
    counter = desk.metrics.counter("soak_total", "soak total")

    for _i in range(100_000):
        counter.inc()
    assert counter.total() == 100_000


def test_counter_labeled_sustained_inc_no_drift():
    """Sustained inc with labeled buckets -> each bucket exact, sum exact."""
    desk = DeskRuntime()
    counter = desk.metrics.counter("soak_labeled", "soak labeled")

    for _i in range(10_000):
        counter.inc(plugin="p_a")
        counter.inc(plugin="p_b")
    assert counter.value(plugin="p_a") == 10_000
    assert counter.value(plugin="p_b") == 10_000
    assert counter.total() == 20_000


# ── Lifecycle sustained execute without residue ──


async def test_lifecycle_sustained_execute_no_residual():
    """500 sequential executes -> all succeed, active_plugins back to baseline with no residue."""
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle

    reg = _registry()
    lifecycle = PluginLifecycle(reg)
    lifecycle.load("soak_p")
    await lifecycle.enable("soak_p")

    active_gauge = reg.desk.metrics.gauge("active_plugins", "active")

    for _i in range(500):
        await lifecycle.execute("soak_p", {})

    # All executions done; active plugin count stable at 1 (enabled, no concurrency residue)
    assert active_gauge.value() == 1


# ── token_meter sustained record + query consistency ──


def test_token_meter_sustained_record_query_consistent():
    """Query after sustained record -> total/aggregation consistent, no loss."""
    desk = DeskRuntime()
    meter = TokenMeter(desk, max_records=10_000)

    for _i in range(8_000):
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
