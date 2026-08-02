"""Claude Code Skill 适配器。

将 PluginManifest 导出为 Claude Code Skill 格式：
- SKILL.md（YAML frontmatter + body）
- references/（参考文档）
- scripts/（可执行脚本）

替代旧 claude_adapter.py，输出符合 2026 Claude Code Plugin 规范。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import PARAM_TYPE_TO_JSON_SCHEMA, PluginParamType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillBundle:
    """Claude Code Skill 包。"""

    skill_md: str
    references: dict[str, str] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)


class SkillAdapter:
    """插件 → Claude Code Skill 导出器。

    用法：
        adapter = SkillAdapter(registry)
        bundle = adapter.export_skill("caveman_compress")
        bundles = adapter.export_all()
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def export_skill(self, plugin_id: str) -> SkillBundle | None:
        manifest = self._registry.get(plugin_id)
        if manifest is None:
            logger.warning("skill_adapter: plugin %s not found", plugin_id)
            return None
        return self._build_bundle(manifest)

    def export_all(self) -> list[SkillBundle]:
        bundles: list[SkillBundle] = []
        for manifest in self._registry.list():
            if PluginCapability.MCP_TOOL not in manifest.capabilities:
                continue
            bundle = self._build_bundle(manifest)
            if bundle is not None:
                bundles.append(bundle)
        return bundles

    def export_default_mounted(self) -> list[SkillBundle]:
        bundles: list[SkillBundle] = []
        for manifest in self._registry.default_mounted():
            bundle = self._build_bundle(manifest)
            if bundle is not None:
                bundles.append(bundle)
        return bundles

    def _build_bundle(self, manifest: PluginManifest) -> SkillBundle:
        skill_md = self._generate_skill_md(manifest)
        references = self._generate_references(manifest)
        scripts = self._generate_scripts(manifest)
        return SkillBundle(
            skill_md=skill_md,
            references=references,
            scripts=scripts,
        )

    def _generate_skill_md(self, manifest: PluginManifest) -> str:
        frontmatter = self._build_frontmatter(manifest)
        body = self._build_skill_body(manifest)
        return f"---\n{frontmatter}\n---\n\n{body}"

    def _build_frontmatter(self, manifest: PluginManifest) -> str:
        lines = [
            f"name: {manifest.id}",
            f"description: {self._escape_yaml(manifest.description)}",
            f"version: {manifest.version}",
        ]
        if manifest.category:
            lines.append(f"category: {manifest.category.value}")
        if manifest.capabilities:
            caps = ", ".join(c.value for c in manifest.capabilities)
            lines.append(f"capabilities: [{caps}]")
        return "\n".join(lines)

    def _build_skill_body(self, manifest: PluginManifest) -> str:
        parts: list[str] = []
        parts.append(f"# {manifest.name}\n")
        parts.append(f"{manifest.description}\n")

        if manifest.params:
            parts.append("\n## Parameters\n")
            for param in manifest.params:
                req = " (required)" if param.required else ""
                parts.append(
                    f"- **{param.name}** `{param.type.value}`{req}: {param.description}"
                )
                if param.enum:
                    parts.append(
                        f"  - Options: {', '.join(str(e) for e in param.enum)}"
                    )
                if param.default is not None:
                    parts.append(f"  - Default: `{param.default}`")

        input_schema = self._build_input_schema(manifest)
        if input_schema:
            parts.append("\n## Input Schema\n")
            parts.append("```json")
            parts.append(json.dumps(input_schema, indent=2, ensure_ascii=False))
            parts.append("```")

        return "\n".join(parts)

    def _build_input_schema(self, manifest: PluginManifest) -> dict[str, Any]:
        if not manifest.params:
            return {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in manifest.params:
            prop: dict[str, Any] = {
                "type": PARAM_TYPE_TO_JSON_SCHEMA.get(
                    param.type, PluginParamType.STRING
                ),
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = list(param.enum)
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema

    def _generate_references(self, manifest: PluginManifest) -> dict[str, str]:
        refs: dict[str, str] = {}
        if manifest.description:
            refs["overview.md"] = f"# {manifest.name}\n\n{manifest.description}\n"
        return refs

    def _generate_scripts(self, manifest: PluginManifest) -> dict[str, str]:
        scripts: dict[str, str] = {}
        return scripts

    @staticmethod
    def _escape_yaml(value: str) -> str:
        if any(c in value for c in (":", "#", "'", '"', "\n")):
            escaped = value.replace("'", "''")
            return f"'{escaped}'"
        return value

    # ── 向后兼容：保留旧 ClaudeSkillAdapter 接口 ──

    def export_one(self, plugin_id: str) -> dict[str, Any] | None:
        """旧接口兼容：返回 dict 格式 Skill 描述。"""
        manifest = self._registry.get(plugin_id)
        if manifest is None:
            return None
        return self._manifest_to_skill_dict(manifest)

    def _manifest_to_skill_dict(self, manifest: PluginManifest) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in manifest.params:
            prop: dict[str, Any] = {
                "type": PARAM_TYPE_TO_JSON_SCHEMA.get(
                    param.type, PluginParamType.STRING
                ),
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = list(param.enum)
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required
        return {
            "name": manifest.id,
            "description": manifest.description,
            "input_schema": input_schema,
            "_fusion": {
                "plugin_name": manifest.name,
                "version": manifest.version,
                "category": manifest.category.value,
                "capabilities": [c.value for c in manifest.capabilities],
                "default_mounted": manifest.default_mounted,
                "timeout_seconds": manifest.timeout_seconds,
            },
        }
