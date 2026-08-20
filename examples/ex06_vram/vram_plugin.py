"""Example 06 - VRAM consumer.

VRAM_CONSUMER + vram_mb. lifecycle.enable() auto-acquires VRAM via
desk.acquire_vram; disable() releases. INLINE mode required (PROCESS
sandbox raises NotImplementedError for VRAM).
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode

logger = logging.getLogger(__name__)


def heavy_compute(desk, params):
    size = int(params.get("matrix_size", 64))
    if desk is not None:
        usage = desk.vram_usage()
        desk.log("heavy_compute", "INFO", "computed", size=size, vram=usage)
    return {"matrix_size": size, "result_ok": True}


HEAVY_COMPUTE_MANIFEST = PluginManifest(
    id="heavy_compute",
    name="Heavy Compute",
    version="0.1.0",
    category=PluginCategory.MLX_INFERENCE,
    description="VRAM-consuming compute (auto acquire/release on enable/disable).",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.VRAM_CONSUMER),
    params=(
        PluginParam(
            name="matrix_size",
            type=PluginParamType.INT,
            description="Square matrix side length.",
            required=False,
            default=64,
        ),
    ),
    entry_point=heavy_compute,
    timeout_seconds=120,
    vram_mb=512,
    sandbox_mode=SandboxMode.INLINE,
)
