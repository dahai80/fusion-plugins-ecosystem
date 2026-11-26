# Plugin → Claude Skill auto-conversion.
# Maps to PRD "auto-convert plugins to Claude Skills": all ecosystem plugins
# get tool descriptions conforming to the Claude Skill spec generated
# automatically, no dedicated adaptation layer needed.
# Claude Skill spec highlights:
# - name: lowercase_with_underscores, globally unique
# - description: one-liner used by Claude to decide when to call
# - input_schema: JSON Schema describing parameters
# - supports long-running tasks and multi-turn tool calls

from __future__ import annotations

import logging
from typing import Any

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)

logger = logging.getLogger(__name__)


class ClaudeSkillAdapter:
    # Plugin → Claude Skill auto-converter.
    # Usage:
    #     adapter = ClaudeSkillAdapter(registry)
    #     skills = adapter.export_all()        # all plugins as Skills
    #     skill = adapter.export_one("caveman_compress")
    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def export_one(self, plugin_id: str) -> dict[str, Any] | None:
        # Export a single plugin as a Claude Skill description.
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return None
        return self._manifest_to_skill(manifest)

    def export_all(self) -> list[dict[str, Any]]:
        # Export all plugins with the CLAUDE_SKILL capability as a Claude
        # Skill list.
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            if PluginCapability.CLAUDE_SKILL not in manifest.capabilities:
                continue
            skill = self._manifest_to_skill(manifest)
            if skill is not None:
                skills.append(skill)
        return skills

    def _manifest_to_skill(self, manifest: PluginManifest) -> dict[str, Any]:
        # Convert a PluginManifest into a Claude Skill description.
        # E7: delegate to skill_adapter's SSOT to avoid input_schema logic
        # drift between two copies.
        from fusion_plugins_ecosystem.skill_adapter import build_skill_dict

        return build_skill_dict(manifest)

    def export_default_mounted(self) -> list[dict[str, Any]]:
        # Export Skills of all plugins with default_mounted=True (mounted to
        # Claude sessions by default).
        # Maps to PRD "builtin caveman and other token compression plugins
        # mounted to Claude sessions by default".
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.default_mounted():
            skill = self._manifest_to_skill(manifest)
            if skill is not None:
                skills.append(skill)
        return skills
