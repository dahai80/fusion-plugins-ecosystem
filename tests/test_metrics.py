"""可观测性指标采集回归测试。

覆盖 metrics.py（Counter/Gauge/MetricsRegistry/标签转义）+ desk_runtime 注入 +
transport /metrics 端点暴露 + 关键路径计数（lifecycle.execute、MCP tools/call、
sandbox spawn、活跃插件/会话 gauge、显存 gauge）。
"""

from __future__ import annotations

import asyncio

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.metrics import (
    MetricsRegistry,
    _escape_label_value,
    _format_labels,
)
from fusion_plugins_ecosystem.transport import _is_metrics_request


# ── Counter / Gauge 基础语义 ──


def test_counter_inc_and_total() -> None:
    r = MetricsRegistry()
    c = r.counter("calls_total", "calls")
    c.inc(plugin="a")
    c.inc(plugin="a")
    c.inc(plugin="b")
    assert c.value(plugin="a") == 2
    assert c.value(plugin="b") == 1
    assert c.total() == 3


def test_counter_rejects_negative() -> None:
    import pytest

    r = MetricsRegistry()
    c = r.counter("c", "c")
    with pytest.raises(ValueError):
        c.inc(-1)


def test_counter_register_is_idempotent() -> None:
    r = MetricsRegistry()
    c1 = r.counter("same", "h")
    c2 = r.counter("same", "h")
    assert c1 is c2
    c1.inc()
    assert c2.total() == 1


def test_gauge_set_inc_dec() -> None:
    r = MetricsRegistry()
    g = r.gauge("active", "active")
    g.set(5)
    assert g.value() == 5
    g.inc(3)
    assert g.value() == 8
    g.dec(2)
    assert g.value() == 6


# ── Prometheus 文本暴露格式 ──


def test_render_empty_registry() -> None:
    r = MetricsRegistry()
    assert r.render() == "\n"


def test_render_counter_empty_bucket_emits_zero() -> None:
    r = MetricsRegistry()
    r.counter("never_inc", "never incremented")
    text = r.render()
    assert "# HELP never_inc never incremented" in text
    assert "# TYPE never_inc counter" in text
    assert "never_inc 0" in text


def test_render_labeled_counter() -> None:
    r = MetricsRegistry()
    c = r.counter("exec_total", "exec")
    c.inc(plugin="compress", status="success")
    text = r.render()
    assert 'exec_total{plugin="compress",status="success"} 1.0' in text


def test_render_orders_counter_then_gauge() -> None:
    r = MetricsRegistry()
    r.gauge("z_gauge", "z")
    r.counter("a_counter", "a")
    text = r.render()
    # 计数器在前，仪表在后（各按名排序）
    assert text.index("a_counter") < text.index("z_gauge")


# ── 标签转义 ──


def test_escape_label_value() -> None:
    assert _escape_label_value('a"b') == 'a\\"b'
    assert _escape_label_value("a\nb") == "a\\nb"
    assert _escape_label_value("a\\b") == "a\\\\b"


def test_format_labels_empty_is_empty_string() -> None:
    assert _format_labels({}) == ""


def test_format_labels_sorted() -> None:
    out = _format_labels({"b": "2", "a": "1"})
    assert out == '{a="1",b="2"}'


# ── DeskRuntime 注入 ──


def test_desk_metrics_lazy_singleton() -> None:
    """desk.metrics 惰性单例：多次访问返回同一对象。"""
    d = DeskRuntime()
    assert d.metrics is d.metrics
    assert isinstance(d.metrics, MetricsRegistry)


# ── transport /metrics 端点短路 ──


def test_is_metrics_request() -> None:
    assert _is_metrics_request("GET /metrics HTTP/1.1") is True
    assert _is_metrics_request("GET /metrics/ HTTP/1.1") is True
    assert _is_metrics_request("POST /metrics HTTP/1.1") is False
    assert _is_metrics_request("GET /health HTTP/1.1") is False
    assert _is_metrics_request("") is False


async def test_http_metrics_endpoint_exposes_exposition() -> None:
    """GET /metrics 返回 Prometheus 文本暴露格式，未鉴权可访问。"""
    from fusion_plugins_ecosystem.transport import HTTPTransport

    d = DeskRuntime()
    d.metrics.counter("mcp_tool_calls_total", "calls").inc(plugin="p", status="success")
    t = HTTPTransport(
        handler=None,
        host="127.0.0.1",
        port=0,
        metrics_provider=lambda: d.metrics.render(),
    )
    await t.start()
    port = t.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    resp = await asyncio.wait_for(reader.read(-1), timeout=5)
    writer.close()
    await writer.wait_closed()
    await t.stop()
    assert b"200 OK" in resp
    assert b"text/plain" in resp
    assert b"# HELP mcp_tool_calls_total" in resp
    assert b"# TYPE mcp_tool_calls_total counter" in resp
    assert b'mcp_tool_calls_total{plugin="p",status="success"}' in resp


async def test_http_metrics_endpoint_no_provider_falls_through() -> None:
    """无 metrics_provider 时 GET /metrics 不短路，落到正常 POST 校验 → 400。"""
    from fusion_plugins_ecosystem.transport import HTTPTransport

    t = HTTPTransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    resp_line = await reader.readline()
    rest = await asyncio.wait_for(reader.read(-1), timeout=5)
    writer.close()
    await writer.wait_closed()
    await t.stop()
    # 无 provider 短路 → 继续 POST 校验 → 非 POST → 400
    assert b"400 Bad Request" in resp_line
    assert b"Connection: close" in rest


# ── 关键路径计数：lifecycle.execute ──


async def test_lifecycle_execute_increments_counter() -> None:
    """execute 成功/超时/错误各入对应 status 桶。"""
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
        PluginRegistry,
    )
    from fusion_plugins_ecosystem.schema import SandboxMode

    def ok_entry(_desk, _params):
        return {"ok": True}

    def boom_entry(_desk, _params):
        raise ValueError("boom")

    def make(pid, entry):
        m = PluginManifest(
            id=pid,
            name=pid,
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="t",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=entry,
            sandbox_mode=SandboxMode.INLINE,
        )
        reg = PluginRegistry()
        reg.register(m)
        return reg, m

    # success
    reg, _ = make("ok_plugin", ok_entry)
    lc = PluginLifecycle(reg)
    await lc.enable("ok_plugin")
    await lc.execute("ok_plugin", {})
    assert (
        reg.desk.metrics.get_counter("plugin_executions_total").value(
            plugin="ok_plugin", status="success"
        )
        == 1
    )

    # error
    reg2, _ = make("boom_plugin", boom_entry)
    lc2 = PluginLifecycle(reg2)
    await lc2.enable("boom_plugin")
    try:
        await lc2.execute("boom_plugin", {})
    except ValueError:
        pass
    assert (
        reg2.desk.metrics.get_counter("plugin_executions_total").value(
            plugin="boom_plugin", status="error"
        )
        == 1
    )
    assert (
        reg2.desk.metrics.get_counter("plugin_errors_total").value(
            plugin="boom_plugin", kind="ValueError"
        )
        == 1
    )


# ── 关键路径计数：MCP tools/call ──


async def test_mcp_tools_call_increments_counter() -> None:
    from fusion_plugins_ecosystem.jsonrpc import MCPHandler
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
        PluginRegistry,
    )
    from fusion_plugins_ecosystem.schema import SandboxMode

    def entry(_desk, _params):
        return {"reply": "ok"}

    m = PluginManifest(
        id="mcp_counter_plugin",
        name="mcp",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="t",
        capabilities=[PluginCapability.MCP_TOOL],
        entry_point=entry,
        sandbox_mode=SandboxMode.INLINE,
    )
    reg = PluginRegistry()
    reg.register(m)
    handler = MCPHandler(registry=reg)
    await handler.lifecycle.enable("mcp_counter_plugin")
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp__plugin__mcp_counter_plugin", "arguments": {}},
        }
    )
    assert resp["result"]["isError"] is False
    assert (
        reg.desk.metrics.get_counter("mcp_tool_calls_total").value(
            plugin="mcp_counter_plugin", status="success"
        )
        == 1
    )


# ── 关键路径 gauge：活跃插件数 ──


async def test_active_plugins_gauge_reflects_enable_disable() -> None:
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
    from fusion_plugins_ecosystem.registry import (
        PluginCategory,
        PluginManifest,
        PluginRegistry,
    )
    from fusion_plugins_ecosystem.schema import SandboxMode

    def entry(_desk, _params):
        return {}

    m = PluginManifest(
        id="gauge_plugin",
        name="g",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="t",
        entry_point=entry,
        sandbox_mode=SandboxMode.INLINE,
    )
    reg = PluginRegistry()
    reg.register(m)
    lc = PluginLifecycle(reg)
    assert reg.desk.metrics.get_gauge("active_plugins").value() == 0
    await lc.enable("gauge_plugin")
    assert reg.desk.metrics.get_gauge("active_plugins").value() == 1
    await lc.disable("gauge_plugin")
    assert reg.desk.metrics.get_gauge("active_plugins").value() == 0


# ── 关键路径 gauge：显存 ──


def test_vram_gauge_reflects_acquire_release() -> None:
    d = DeskRuntime()
    d.vram_total_mb = 1024
    d.acquire_vram("vram_plugin", 100)
    assert d.metrics.get_gauge("vram_used_mb").value() == 100
    d.acquire_vram("vram_plugin2", 200)
    assert d.metrics.get_gauge("vram_used_mb").value() == 300
    d.release_vram("vram_plugin")
    assert d.metrics.get_gauge("vram_used_mb").value() == 200


# ── 关键路径计数：sandbox spawn ──


async def test_sandbox_spawn_increments_counter() -> None:
    """PROCESS 沙箱 spawn 入 sandbox_spawns_total 计数。"""
    from fusion_plugins_ecosystem.sandbox import PluginSandbox, ResourceLimits

    d = DeskRuntime()
    sandbox = PluginSandbox(desk=d)

    # 用本测试模块自身作为模块级入口（PROCESS 须模块级属性）
    def _no_call():
        pass

    # 注册一个模块级可导入的入口：复用本模块已存在符号
    import tests.test_metrics as _self_mod

    await sandbox.spawn(
        "spawn_count_plugin",
        entry_point=f"{_self_mod.__name__}:test_sandbox_spawn_increments_counter",
        config={},
        limits=ResourceLimits(timeout_seconds=5),
    )
    assert (
        d.metrics.get_counter("sandbox_spawns_total").value(
            plugin="spawn_count_plugin", status="spawned"
        )
        == 1
    )
    await sandbox.kill("spawn_count_plugin")
    await sandbox.shutdown_all()
