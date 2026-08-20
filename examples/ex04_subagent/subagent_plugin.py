"""Example 04 - reverse subagent dispatch.

Plugin has SUBAGENT capability so ClaudeGateway can pull a Claude Code
subagent to run it via dispatch_subagent(SubagentTask(...)).
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


def batch_refactor(desk, params):
    target = params.get("target", "")
    action = params.get("action", "rename")
    if desk is not None:
        desk.log(
            "batch_refactor", "INFO", "subagent task", target=target, action=action
        )
    return {"refactored": target, "action": action, "files_touched": 1}


BATCH_REFACTOR_MANIFEST = PluginManifest(
    id="batch_refactor",
    name="Batch Refactor",
    version="0.1.0",
    category=PluginCategory.CODING_PLAN,
    description="Subagent-driven batch refactor of a target symbol.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.SUBAGENT),
    params=(
        PluginParam(
            name="target",
            type=PluginParamType.STRING,
            description="Symbol or path to refactor.",
            required=True,
        ),
        PluginParam(
            name="action",
            type=PluginParamType.STRING,
            description="Refactor action.",
            required=False,
            default="rename",
            enum=("rename", "extract", "inline"),
        ),
    ),
    entry_point=batch_refactor,
    timeout_seconds=120,
    agent_model="claude-fable-5",
)
