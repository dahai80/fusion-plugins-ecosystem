"""Example 07 - long task with timeout meltdown + auto-restart.

LONG_TASK capability + short timeout_seconds. On timeout the plugin
transitions to TIMEOUT and _maybe_restart reloads it (up to max_restart).
The test forces a timeout via execute(timeout_override=) with a sleep
longer than the override.
"""

from __future__ import annotations

import asyncio
import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


async def slow_worker(desk, params):
    work_seconds = float(params.get("work_seconds", 0.1))
    if desk is not None:
        desk.log("slow_worker", "INFO", "starting", work_seconds=work_seconds)
    await asyncio.sleep(work_seconds)
    return {"done": True, "worked": work_seconds}


SLOW_WORKER_MANIFEST = PluginManifest(
    id="slow_worker",
    name="Slow Worker",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Long-running worker; demonstrates timeout meltdown + restart.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.LONG_TASK),
    params=(
        PluginParam(
            name="work_seconds",
            type=PluginParamType.FLOAT,
            description="Seconds of simulated work.",
            required=False,
            default=0.1,
        ),
    ),
    entry_point=slow_worker,
    timeout_seconds=60,
    max_restart=3,
)
