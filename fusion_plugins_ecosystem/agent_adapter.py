"""Claude Code Agent 适配器。

将具备 SUBAGENT 能力的 PluginManifest 导出为 Claude Code agent .md 文件，
含 YAML frontmatter（name, description, model, color, tools）。
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)

logger = logging.getLogger(__name__)

_CATEGORY_COLORS: dict[PluginCategory, str] = {
    PluginCategory.CODING_PLAN: "#4A90D9",
    PluginCategory.CONTEXT_COMPRESS: "#7B68EE",
    PluginCategory.MLX_INFERENCE: "#FF6B6B",
    PluginCategory.TERMINAL_PROXY: "#FF8C00",
    PluginCategory.FILE_INDEX: "#50C878",
    PluginCategory.QUANTIZATION: "#E67E22",
    PluginCategory.VISUAL_BACKEND: "#9B59B6",
    PluginCategory.CUSTOM: "#95A5A6",
}

_CAPABILITY_TOOLS: dict[PluginCapability, str] = {
    PluginCapability.MCP_TOOL: "mcp",
    PluginCapability.CLAUDE_SKILL: "skill",
    PluginCapability.SUBAGENT: "agent",
    PluginCapability.FILE_ACCESS: "file_access",
    PluginCapability.LONG_TASK: "long_task",
}


class AgentAdapter:
    """插件 → Claude Code Agent .md 导出器。

    用法：
        adapter = AgentAdapter(registry)
        md = adapter.export_agent("my_agent_plugin")
        all_agents = adapter.export_all()
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def export_agent(self, plugin_id: str) -> str | None:
        manifest = self._registry.get(plugin_id)
        if manifest is None:
            logger.warning("agent_adapter: plugin %s not found", plugin_id)
            return None
        if PluginCapability.SUBAGENT not in manifest.capabilities:
            logger.info(
                "agent_adapter: %s lacks SUBAGENT capability, skipping", plugin_id
            )
            return None
        return self._build_agent_md(manifest)

    def export_all(self) -> list[str]:
        agents: list[str] = []
        for manifest in self._registry.list():
            if PluginCapability.SUBAGENT not in manifest.capabilities:
                continue
            md = self._build_agent_md(manifest)
            if md is not None:
                agents.append(md)
        return agents

    def _build_agent_md(self, manifest: PluginManifest) -> str:
        frontmatter = self._build_frontmatter(manifest)
        body = self._build_agent_body(manifest)
        return f"---\n{frontmatter}\n---\n\n{body}"

    def _build_frontmatter(self, manifest: PluginManifest) -> str:
        lines = [
            f"name: {manifest.id}",
            f"description: {self._escape_yaml(manifest.description)}",
            "model: inherit",
            f"color: {self._category_to_color(manifest.category)}",
        ]
        tools = self._capability_to_tools(manifest.capabilities)
        if tools:
            lines.append(f"tools: [{', '.join(tools)}]")
        return "\n".join(lines)

    def _build_agent_body(self, manifest: PluginManifest) -> str:
        parts: list[str] = []
        parts.append(f"# {manifest.name}\n")
        parts.append(f"{manifest.description}\n")

        if manifest.params:
            parts.append("\n## Configuration\n")
            for param in manifest.params:
                req = " (required)" if param.required else ""
                parts.append(
                    f"- **{param.name}** `{param.type.value}`{req}: {param.description}"
                )

        parts.append("\n## Behavior\n")
        parts.append(
            f"This agent wraps plugin `{manifest.id}` "
            f"(v{manifest.version}, {manifest.category.value}). "
            f"It executes the plugin entry point with the provided parameters."
        )

        return "\n".join(parts)

    @staticmethod
    def _category_to_color(category: PluginCategory) -> str:
        return _CATEGORY_COLORS.get(category, "#95A5A6")

    @staticmethod
    def _capability_to_tools(capabilities: tuple[PluginCapability, ...]) -> list[str]:
        tools: list[str] = []
        for cap in capabilities:
            tool_name = _CAPABILITY_TOOLS.get(cap)
            if tool_name and tool_name not in tools:
                tools.append(tool_name)
        return tools

    @staticmethod
    def _escape_yaml(value: str) -> str:
        if any(c in value for c in (":", "#", "'", '"', "\n")):
            escaped = value.replace("'", "''")
            return f"'{escaped}'"
        return value
