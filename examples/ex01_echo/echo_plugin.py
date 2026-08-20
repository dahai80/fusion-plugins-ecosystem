"""Example 01 - minimal synchronous plugin.

Entry point: module-level function echo(desk, params) -> dict.
Capabilities: MCP_TOOL + CLAUDE_SKILL (auto-exposes as MCP tool + Skill).
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


def echo(desk, params):
    text = params.get("text", "")
    if desk is not None:
        desk.log("echo", "INFO", "echoed", chars=len(text))
    return {"echo": text.upper()}


ECHO_MANIFEST = PluginManifest(
    id="echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Echoes input text uppercased.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL),
    params=(
        PluginParam(
            name="text",
            type=PluginParamType.STRING,
            description="Input text to echo.",
            required=True,
        ),
    ),
    entry_point=echo,
    timeout_seconds=30,
)
