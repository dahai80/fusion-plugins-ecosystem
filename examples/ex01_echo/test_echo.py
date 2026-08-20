"""Example 01 - tests for echo plugin."""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.lifecycle import PluginState
from fusion_plugins_ecosystem.skill_adapter import SkillAdapter

from examples.ex01_echo.echo_plugin import ECHO_MANIFEST


async def test_echo_register_enable_execute():
    registry = fpe.PluginRegistry()
    registry.register(ECHO_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)

    await lifecycle.enable("echo")
    assert lifecycle.get_state("echo")["state"] == PluginState.ENABLED.value

    result = await lifecycle.execute("echo", {"text": "hello"})
    assert result == {"echo": "HELLO"}

    await lifecycle.disable("echo")
    lifecycle.unload("echo")


async def test_echo_mcp_tool_exposed():
    registry = fpe.PluginRegistry()
    registry.register(ECHO_MANIFEST)
    gw = fpe.ClaudeGateway(registry, fpe.PluginLifecycle(registry))

    tools = gw.list_mcp_tools()
    ids = {t["name"] for t in tools}
    assert "mcp__plugin__echo" in ids


async def test_echo_skill_exported():
    registry = fpe.PluginRegistry()
    registry.register(ECHO_MANIFEST)
    bundle = SkillAdapter(registry).export_skill("echo")

    assert bundle is not None
    assert "name: echo" in bundle.skill_md
    assert "## Parameters" in bundle.skill_md
    assert "**text**" in bundle.skill_md
