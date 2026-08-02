"""Claude Code Plugin 包生成器。

生成完整 .claude-plugin/ 目录结构：
    <plugin-id>/
        .claude-plugin/
            plugin.json
        skills/
            <skill-id>/
                SKILL.md
                references/
        agents/
            <agent-id>.md
        .mcp.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fusion_plugins_ecosystem.agent_adapter import AgentAdapter
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import MCP_PROTOCOL_VERSION
from fusion_plugins_ecosystem.skill_adapter import SkillBundle, SkillAdapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginBundle:
    """Claude Code Plugin 包。"""

    plugin_id: str
    plugin_json: str
    skills: dict[str, SkillBundle] = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)
    mcp_config: str = ""


class PluginBundleGenerator:
    """Claude Code Plugin 包生成器。

    用法：
        gen = PluginBundleGenerator(registry)
        bundle = gen.generate("caveman_compress")
        bundles = gen.generate_all()
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._skill_adapter = SkillAdapter(registry)
        self._agent_adapter = AgentAdapter(registry)

    def generate(self, plugin_id: str) -> PluginBundle | None:
        manifest = self._registry.get(plugin_id)
        if manifest is None:
            logger.warning("plugin_bundle: plugin %s not found", plugin_id)
            return None
        return self._build_bundle(manifest)

    def generate_all(self) -> list[PluginBundle]:
        bundles: list[PluginBundle] = []
        for manifest in self._registry.list():
            bundle = self._build_bundle(manifest)
            if bundle is not None:
                bundles.append(bundle)
        return bundles

    def _build_bundle(self, manifest: PluginManifest) -> PluginBundle:
        plugin_json = self._generate_plugin_json(manifest)

        skills: dict[str, SkillBundle] = {}
        skill_bundle = self._skill_adapter.export_skill(manifest.id)
        if skill_bundle is not None:
            skills[manifest.id] = skill_bundle

        agents: list[str] = []
        if PluginCapability.SUBAGENT in manifest.capabilities:
            agent_md = self._agent_adapter.export_agent(manifest.id)
            if agent_md is not None:
                agents.append(agent_md)

        mcp_config = self._generate_mcp_config(manifest)

        return PluginBundle(
            plugin_id=manifest.id,
            plugin_json=plugin_json,
            skills=skills,
            agents=agents,
            mcp_config=mcp_config,
        )

    def _generate_plugin_json(self, manifest: PluginManifest) -> str:
        data: dict[str, Any] = {
            "name": manifest.id,
            "version": manifest.version,
            "description": manifest.description,
            "category": manifest.category.value,
            "capabilities": [c.value for c in manifest.capabilities],
        }
        if manifest.params:
            data["params"] = [
                {
                    "name": p.name,
                    "type": p.type.value,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": list(p.enum) if p.enum else None,
                }
                for p in manifest.params
            ]
        return json.dumps(data, indent=2, ensure_ascii=False)

    def _generate_mcp_config(self, manifest: PluginManifest) -> str:
        has_mcp = PluginCapability.MCP_TOOL in manifest.capabilities
        if not has_mcp:
            return ""
        data: dict[str, Any] = {
            "mcpServers": {
                manifest.id: {
                    "command": "fusion-plugin-server",
                    "args": ["--transport", "stdio"],
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                }
            }
        }
        return json.dumps(data, indent=2, ensure_ascii=False)
