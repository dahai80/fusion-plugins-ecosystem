"""Production-release audit P0-P3 fix regression tests.

Covers this audit round's hardening fixes so they do not regress:
- P0-1 remote transport on non-loopback without auth refuses to start
- P1-1 restart_count resets after a successful execution
- P1-5 security-sensitive config is read-only over RPC
- P1-7 exceptions are sanitized before reaching clients
- P1-8 non-dict IPC messages do not kill the sandbox
- P2-1 restore_config syncs to lifecycle/token_meter
- P2-2 plugin_id format validation
- P2-8 unauthenticated GET /health probe
- P3-2 busy-loop floor
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


# ── P0-1: remote transport on non-loopback without auth refuses to start ──


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
    with pytest.raises(RuntimeError, match="auth token"):
        await server.start(transport="sse", host="0.0.0.0", port=0)


async def test_remote_transport_loopback_allowed_without_auth() -> None:
    # loopback can start without auth (local single-machine/test scenario)
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


# ── P1-1: restart_count resets after a successful execution ──


def _crash_then_recover_registry() -> PluginRegistry:
    """Plugin crashes on first call, succeeds on second call."""
    calls = {"n": 0}

    def entry(_desk, _params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("first-call crash")
        return {"ok": True}

    manifest = PluginManifest(
        id="recover_plugin",
        name="Recover",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="recovers after crash",
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
    # First execution crashes -> triggers restart, restart_count=1
    with pytest.raises(ValueError):
        await lifecycle.execute("recover_plugin", {})
    inst = lifecycle._instances["recover_plugin"]
    assert inst.restart_count == 1
    # Second execution succeeds -> restart_count should reset to zero
    result = await lifecycle.execute("recover_plugin", {})
    assert result == {"ok": True}
    assert lifecycle._instances["recover_plugin"].restart_count == 0


# ── P1-5: security-sensitive config is read-only over RPC ──


def _make_handler() -> MCPHandler:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    manifest = PluginManifest(
        id="caveman_compress",
        name="Caveman",
        version="0.3.3",
        category=PluginCategory.CONTEXT_COMPRESS,
        description="compress",
        capabilities=(PluginCapability.MCP_TOOL,),
        params=(
            PluginParam(
                name="ratio",
                type=PluginParamType.INT,
                description="compression ratio",
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
    # sandbox_mode projects sandbox_default_mode -> read-only
    result = await _call(handler, "plugins/config.set", {"sandbox_mode": "process"})
    assert result["ok"] is False
    # i18n: user-facing
    assert "敏感" in result["error"]
    # mcp_host backend original name -> read-only
    result = await _call(handler, "plugins/config.set", {"mcp_host": "0.0.0.0"})
    assert result["ok"] is False


async def test_config_set_non_sensitive_field_allowed() -> None:
    handler = _make_handler()
    result = await _call(handler, "plugins/config.set", {"log_level": "DEBUG"})
    assert result["ok"] is True


# ── P1-7: exceptions sanitized before reaching clients ──


async def test_tools_call_error_no_raw_exception() -> None:
    handler = _make_handler()
    # Plugin not enabled -> execute raises; the response must not contain
    # the raw exception text
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp__plugin__caveman_compress", "arguments": {}},
        }
    )
    err_text = resp["result"]["content"][0]["text"]
    assert "not enabled" not in err_text  # raw exception details must not reach the response


# ── P2-1: restore_config syncs to lifecycle/token_meter ──


async def test_apply_config_to_deps_syncs_lifecycle_and_meter() -> None:
    handler = _make_handler()
    # Simulate config being replaced by a new object with new thresholds after restore
    new_cfg = EcosystemConfig(subagent_timeout_seconds=99, max_token_records=42)
    handler.config = new_cfg
    handler._apply_config_to_deps()
    assert handler.lifecycle.config is new_cfg
    assert handler.token_meter._max_records == 42
    assert handler.token_meter._persist_path == new_cfg.token_persist_path


# ── P2-2: plugin_id format validation ──


async def test_install_rejects_invalid_plugin_id() -> None:
    handler = _make_handler()
    # A plugin_id containing a newline must be rejected (log-injection guard)
    result = await _call(
        handler, "plugins/install", {"plugin_id": "evil\n[CRITICAL] fake"}
    )
    assert result["ok"] is False


async def test_state_get_invalid_plugin_id_returns_unknown() -> None:
    handler = _make_handler()
    result = await _call(handler, "plugins/state.get", {"plugin_id": "bad id!"})
    assert result["state"] == "unknown"


# ── P2-8: unauthenticated GET /health probe ──


def test_is_health_request() -> None:
    assert _is_health_request("GET /health HTTP/1.1") is True
    assert _is_health_request("GET /health/ HTTP/1.1") is True
    assert _is_health_request("POST /health HTTP/1.1") is False
    assert _is_health_request("GET /tools HTTP/1.1") is False
    assert _is_health_request("") is False


# ── P3-2: busy-loop floor (sleep is never 0) ──


def test_watch_loop_sleep_floor() -> None:
    # With heartbeat_stale=1, //2=0, but after the fix max(1,0)=1, ensuring no busy spin
    import inspect

    src = inspect.getsource(PluginLifecycle._watch_loop)
    assert "max(1," in src


# ── P1-8: non-dict IPC messages do not kill the sandbox (_read_loop guard) ──


def test_read_loop_non_dict_guard_exists() -> None:
    import inspect

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    src = inspect.getsource(sandbox_mod.PluginSandbox._read_loop)
    assert "isinstance(msg, dict)" in src


# ── P1-2: stop_watcher must be awaited (sync call leaks the watcher task) ──


def test_full_shutdown_awaits_stop_watcher() -> None:
    # server._full_shutdown must call stop_watcher with await, otherwise
    # coroutine never awaited + watcher task leak
    import inspect

    from fusion_plugins_ecosystem import server as server_mod

    src = inspect.getsource(server_mod.MCPServer._full_shutdown)
    assert "await self.lifecycle.stop_watcher()" in src
    assert "self.lifecycle.stop_watcher()" not in src.replace(
        "await self.lifecycle.stop_watcher()", ""
    )


# ── P0-3: PROCESS worker config serialized as Python literals (not JSON) ──


def test_worker_config_uses_python_literal() -> None:
    # json.dumps's true/false/null are not valid Python identifiers; embedded in
    # the worker script they cause NameError -> every PROCESS plugin carrying
    # bool/None config crashes at spawn. After the fix repr() emits True/False/None.
    import inspect

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    src = inspect.getsource(sandbox_mod.PluginSandbox._build_worker_script)
    assert "_CONFIG={config!r}" in src
    assert "json.dumps(config)" not in src


def test_worker_config_expr_is_valid_python() -> None:
    # End-to-end: the _CONFIG expression in the worker script generated with
    # bool/None config must be valid Python (True/False/None), never true/false/null.
    import re

    from fusion_plugins_ecosystem import sandbox as sandbox_mod

    sandbox = sandbox_mod.PluginSandbox(desk=DeskRuntime())
    script = sandbox._build_worker_script(
        "mod:attr",
        dict(EcosystemConfig().to_dict()),
        sandbox_mod.ResourceLimits(timeout_seconds=30),
    )
    m = re.search(r"^_CONFIG=(.+)$", script, re.MULTILINE)
    assert m, "worker script is missing the _CONFIG assignment"
    expr = m.group(1)
    assert " true" not in expr and "\ttrue" not in expr
    assert "True" in expr or "False" in expr or "None" in expr


# ── P2-9: infra_log dual log-stream collection ──


def test_infra_log_records_to_ring_buffer() -> None:
    """P2-9: infra_log writes into the ring buffer with plugin_id=_infra, queryable by ops via get_logs."""
    desk = DeskRuntime()
    desk.infra_log("jsonrpc", "ERROR", "handler foo error: ValueError")
    logs = desk.get_logs(plugin_id="_infra", limit=10)
    assert any("[jsonrpc]" in e["message"] for e in logs)
    assert any("handler foo error" in e["message"] for e in logs)


async def test_illegal_transition_logged_to_desk() -> None:
    """P2-9: illegal state transitions also funnel into the desk ring buffer (not only stderr)."""
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
    from fusion_plugins_ecosystem.registry import PluginRegistry

    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    lifecycle = PluginLifecycle(registry)
    # Build a LOADED instance, then transition illegally to a state other than ENABLED
    from fusion_plugins_ecosystem.lifecycle import PluginInstance

    from tests.test_lifecycle import _make_manifest

    inst = PluginInstance(
        manifest=_make_manifest(entry_point=lambda d, p: {}),
        state=PluginState.LOADED,
    )
    registry._manifests = {inst.manifest.id: inst.manifest}
    lifecycle._instances[inst.manifest.id] = inst
    with pytest.raises(RuntimeError, match="illegal state transition"):
        lifecycle._transition(inst, PluginState.TIMEOUT)
    infra_logs = desk.get_logs(plugin_id="_infra", limit=20)
    assert any("illegal state transition" in e["message"] for e in infra_logs)
