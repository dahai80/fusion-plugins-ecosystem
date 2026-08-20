"""Example 06 - tests for VRAM consumer lifecycle.

enable() auto-acquires VRAM (manifest.vram_mb), disable() releases it.
Default DeskRuntime has vram_total_mb=0 (unlimited) so acquire always ok.
"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

from examples.ex06_vram.vram_plugin import HEAVY_COMPUTE_MANIFEST


def _make_registry() -> tuple[fpe.PluginRegistry, fpe.PluginLifecycle]:
    desk = DeskRuntime()
    registry = fpe.PluginRegistry(desk=desk)
    registry.register(HEAVY_COMPUTE_MANIFEST)
    return registry, fpe.PluginLifecycle(registry)


async def test_enable_acquires_vram():
    registry, lifecycle = _make_registry()
    await lifecycle.enable("heavy_compute")

    usage = lifecycle.desk.vram_usage()
    assert usage.get("heavy_compute") == 512

    await lifecycle.disable("heavy_compute")


async def test_execute_returns_result():
    registry, lifecycle = _make_registry()
    await lifecycle.enable("heavy_compute")
    result = await lifecycle.execute("heavy_compute", {"matrix_size": 128})

    assert result == {"matrix_size": 128, "result_ok": True}

    await lifecycle.disable("heavy_compute")


async def test_disable_releases_vram():
    registry, lifecycle = _make_registry()
    await lifecycle.enable("heavy_compute")
    assert lifecycle.desk.vram_usage().get("heavy_compute") == 512

    await lifecycle.disable("heavy_compute")
    assert "heavy_compute" not in lifecycle.desk.vram_usage()

    lifecycle.unload("heavy_compute")
