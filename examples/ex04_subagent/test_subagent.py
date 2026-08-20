"""Example 04 - tests for reverse subagent dispatch."""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.claude_gateway import ClaudeGateway, SubagentTask

from examples.ex04_subagent.subagent_plugin import BATCH_REFACTOR_MANIFEST


def test_subagent_capability_listed():
    registry = fpe.PluginRegistry()
    registry.register(BATCH_REFACTOR_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)
    gw = ClaudeGateway(registry, lifecycle)

    assert "batch_refactor" in gw.list_subagent_capable_plugins()


async def test_dispatch_subagent_completes():
    registry = fpe.PluginRegistry()
    registry.register(BATCH_REFACTOR_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)
    gw = ClaudeGateway(registry, lifecycle)

    task = SubagentTask(
        name="refactor-1",
        plugin_id="batch_refactor",
        arguments={"target": "old_name", "action": "rename"},
        timeout_seconds=30,
    )
    result = await gw.dispatch_subagent(task)

    assert result["task"] == "refactor-1"
    assert result["plugin_id"] == "batch_refactor"
    assert result["state"] == "completed"
    assert result["result"] == {
        "refactored": "old_name",
        "action": "rename",
        "files_touched": 1,
    }


async def test_dispatch_subagent_unknown_plugin_raises():
    # Unknown plugin_id is a registration error, raised before execute → KeyError.
    registry = fpe.PluginRegistry()
    registry.register(BATCH_REFACTOR_MANIFEST)
    gw = ClaudeGateway(registry, fpe.PluginLifecycle(registry))

    task = SubagentTask(name="x", plugin_id="does_not_exist", arguments={})
    try:
        await gw.dispatch_subagent(task)
        assert False, "expected KeyError"
    except KeyError as exc:
        assert "does_not_exist" in str(exc)


async def test_dispatch_subagent_propagates_execution_error():
    # A registered plugin whose entry point raises is caught by
    # dispatch_subagent's try/except and returned as state="failed".
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
    )

    def boom(desk, params):
        raise ValueError("simulated crash")

    boom_manifest = PluginManifest(
        id="boom_subagent",
        name="Boom",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="Always crashes.",
        capabilities=(PluginCapability.MCP_TOOL, PluginCapability.SUBAGENT),
        params=(),
        entry_point=boom,
        timeout_seconds=10,
    )

    registry = fpe.PluginRegistry()
    registry.register(boom_manifest)
    gw = ClaudeGateway(registry, fpe.PluginLifecycle(registry))

    task = SubagentTask(
        name="crash-1", plugin_id="boom_subagent", arguments={}, timeout_seconds=10
    )
    result = await gw.dispatch_subagent(task)

    assert result["task"] == "crash-1"
    assert result["plugin_id"] == "boom_subagent"
    assert result["state"] == "failed"
    assert "simulated crash" in result["error"]
