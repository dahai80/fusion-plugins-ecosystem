"""Example 03 - CLAUDE_SKILL export via SkillAdapter.

SkillAdapter.export_skill(id) -> SkillBundle with skill_md (YAML frontmatter
+ body), references, scripts. Demonstrates the generated Skill format.
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import MCPAnnotations, PluginParamType

logger = logging.getLogger(__name__)


def format_report(desk, params):
    title = params.get("title", "untitled")
    body = params.get("body", "")
    if desk is not None:
        desk.log("format_report", "INFO", "formatted", title=title, chars=len(body))
    return {"markdown": f"# {title}\n\n{body}"}


FORMAT_REPORT_MANIFEST = PluginManifest(
    id="format_report",
    name="Format Report",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Formats a title + body into a markdown report.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL),
    params=(
        PluginParam(
            name="title",
            type=PluginParamType.STRING,
            description="Report title.",
            required=True,
        ),
        PluginParam(
            name="body",
            type=PluginParamType.STRING,
            description="Report body text.",
            required=False,
            default="",
        ),
    ),
    entry_point=format_report,
    timeout_seconds=30,
    mcp_annotations=MCPAnnotations(
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema={
        "type": "object",
        "properties": {"markdown": {"type": "string"}},
        "required": ["markdown"],
    },
)
