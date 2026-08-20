"""Example 02 - async entry point.

lifecycle._invoke_inline auto-detects async via inspect.iscoroutinefunction
and awaits the coroutine. No special registration needed.
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


async def async_fetch(desk, params):
    delay = float(params.get("delay_seconds", 0.05))
    label = params.get("label", "result")
    if desk is not None:
        desk.log("async_fetch", "DEBUG", "sleeping", delay=delay)
    await asyncio.sleep(delay)
    return {"label": label, "waited": delay}


ASYNC_FETCH_MANIFEST = PluginManifest(
    id="async_fetch",
    name="Async Fetch",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Async entry point demo: sleeps then returns a label.",
    capabilities=(PluginCapability.MCP_TOOL,),
    params=(
        PluginParam(
            name="label",
            type=PluginParamType.STRING,
            description="Label echoed in result.",
            required=True,
        ),
        PluginParam(
            name="delay_seconds",
            type=PluginParamType.FLOAT,
            description="Seconds to await.",
            required=False,
            default=0.05,
        ),
    ),
    entry_point=async_fetch,
    timeout_seconds=30,
)
