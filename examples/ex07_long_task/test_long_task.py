"""Example 07 - tests for long task timeout meltdown + auto-restart.

On timeout, lifecycle transitions to TIMEOUT then _maybe_restart reloads
the plugin (unload→load→enable). After restart the plugin is ENABLED again
with restart_count incremented, as long as under max_restart.
"""

from __future__ import annotations

import asyncio

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

from examples.ex07_long_task.long_task_plugin import SLOW_WORKER_MANIFEST


def _make_lifecycle() -> fpe.PluginLifecycle:
    registry = fpe.PluginRegistry(desk=DeskRuntime())
    registry.register(SLOW_WORKER_MANIFEST)
    return fpe.PluginLifecycle(registry)


async def test_slow_worker_normal_execute():
    lifecycle = _make_lifecycle()
    await lifecycle.enable("slow_worker")
    result = await lifecycle.execute("slow_worker", {"work_seconds": 0.02})

    assert result == {"done": True, "worked": 0.02}

    await lifecycle.disable("slow_worker")
    lifecycle.unload("slow_worker")


async def test_timeout_triggers_meltdown_and_restart():
    lifecycle = _make_lifecycle()
    await lifecycle.enable("slow_worker")

    # work_seconds (0.3) far exceeds timeout_override (0.05) → TimeoutError
    try:
        await lifecycle.execute(
            "slow_worker",
            {"work_seconds": 0.3},
            timeout_override=0.05,
        )
        assert False, "expected asyncio.TimeoutError"
    except asyncio.TimeoutError:
        pass

    # _maybe_restart reloaded the plugin → ENABLED with restart_count=1
    inst = lifecycle._instances["slow_worker"]
    assert inst.restart_count == 1
    assert inst.state == fpe.PluginState.ENABLED

    # plugin still usable after restart
    result = await lifecycle.execute("slow_worker", {"work_seconds": 0.02})
    assert result == {"done": True, "worked": 0.02}

    await lifecycle.disable("slow_worker")
    lifecycle.unload("slow_worker")
