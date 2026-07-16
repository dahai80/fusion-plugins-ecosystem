"""DeskContext 桥接层测试。"""

from __future__ import annotations

import logging

import pytest

from fusion_plugins_ecosystem.desk_context import DeskContext
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime


def test_desk_context_post_init_syncs_port() -> None:
    rt = DeskRuntime(mcp_gateway_port=9000)
    ctx = DeskContext(runtime=rt)
    assert ctx.mcp_gateway_port == 9000


def test_desk_context_explicit_port_preserved() -> None:
    rt = DeskRuntime(mcp_gateway_port=9000)
    ctx = DeskContext(runtime=rt, mcp_gateway_port=7000)
    assert ctx.mcp_gateway_port == 7000


def test_desk_context_ensure_runtime_creates_default() -> None:
    ctx = DeskContext()
    assert ctx.runtime is None
    rt = ctx._ensure_runtime()
    assert isinstance(rt, DeskRuntime)
    assert ctx.runtime is rt


def test_desk_context_acquire_vram_delegates() -> None:
    ctx = DeskContext()
    assert ctx.acquire_vram("p1", 100) is True
    assert ctx.vram_usage() == {"p1": 100}


def test_desk_context_release_vram_delegates() -> None:
    ctx = DeskContext()
    ctx.acquire_vram("p1", 50)
    ctx.release_vram("p1")
    assert ctx.vram_usage() == {}


def test_desk_context_log_delegates(caplog: pytest.LogCaptureFixture) -> None:
    ctx = DeskContext()
    with caplog.at_level(logging.INFO, logger="fusion_plugins_ecosystem.desk_runtime"):
        ctx.log("p1", "INFO", "msg")
    assert "msg" in caplog.text


def test_desk_context_check_file_permission_default_true() -> None:
    ctx = DeskContext()
    assert ctx.check_file_permission("p1", "/any") is True


def test_desk_context_grant_permission_then_check() -> None:
    ctx = DeskContext()
    ctx.grant_permission("p1", ["/data/"])
    assert ctx.check_file_permission("p1", "/data/x") is True
    assert ctx.check_file_permission("p1", "/other/x") is False


def test_desk_context_get_api_key_no_config_returns_none() -> None:
    ctx = DeskContext()
    assert ctx.get_api_key("volcengine_claude") is None


def test_desk_context_set_api_key_no_config_noop() -> None:
    ctx = DeskContext()
    ctx.set_api_key("provider", "key")  # 不应抛异常


def test_desk_context_list_nodes_no_registry_empty() -> None:
    ctx = DeskContext()
    assert ctx.list_nodes() == []


def test_desk_context_resolve_node_no_registry_none() -> None:
    ctx = DeskContext()
    assert ctx.resolve_node("file_copy") is None


def test_desk_context_list_scheduled_tasks_no_scheduler_empty() -> None:
    ctx = DeskContext()
    assert ctx.list_scheduled_tasks() == []


async def test_desk_context_mlx_chat_no_client_raises() -> None:
    ctx = DeskContext()
    with pytest.raises(RuntimeError, match="fusion-mlx 客户端未注入"):
        await ctx.mlx_chat("m", [])


async def test_desk_context_mlx_health_no_client_false() -> None:
    ctx = DeskContext()
    assert await ctx.mlx_health() is False


def test_desk_context_gateway_info() -> None:
    rt = DeskRuntime(mcp_gateway_port=8080)
    ctx = DeskContext(runtime=rt)
    info = ctx.gateway_info()
    assert info["port"] == 8080


def test_desk_context_registered_plugin_ids_isolated() -> None:
    ctx = DeskContext()
    ctx.registered_plugin_ids.add("p1")
    assert "p1" in ctx.registered_plugin_ids
    # 确保不影响 runtime
    assert ctx.runtime is None
