"""Example 02 - tests for async entry point."""

from __future__ import annotations

import inspect

import fusion_plugins_ecosystem as fpe

from examples.ex02_async.async_plugin import ASYNC_FETCH_MANIFEST, async_fetch


def test_async_entry_point_is_coroutine():
    assert inspect.iscoroutinefunction(async_fetch)


async def test_async_fetch_executes():
    registry = fpe.PluginRegistry()
    registry.register(ASYNC_FETCH_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)

    await lifecycle.enable("async_fetch")
    result = await lifecycle.execute(
        "async_fetch", {"label": "demo", "delay_seconds": 0.02}
    )
    assert result == {"label": "demo", "waited": 0.02}

    await lifecycle.disable("async_fetch")
    lifecycle.unload("async_fetch")


async def test_async_fetch_default_delay():
    registry = fpe.PluginRegistry()
    registry.register(ASYNC_FETCH_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)

    await lifecycle.enable("async_fetch")
    result = await lifecycle.execute("async_fetch", {"label": "x"})
    assert result["label"] == "x"
    assert result["waited"] == 0.05

    await lifecycle.disable("async_fetch")
    lifecycle.unload("async_fetch")
