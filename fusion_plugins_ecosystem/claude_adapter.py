"""插件 → Claude Skill 自动转换。

对应 PRD「插件自动转 Claude Skill」：
所有生态插件自动生成符合 Claude Skill 规范的工具描述，无需单独写适配层。

Claude Skill 规范要点：
- name: 小写下划线，全局唯一
- description: 一句话描述，供 Claude 决策何时调用
- input_schema: JSON Schema，描述参数
- 支持长任务、多轮工具调用
"""

from __future__ import annotations

import logging
from typing import Any

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)

logger = logging.getLogger(__name__)


# JSON Schema 类型映射
_PARAM_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "int": "integer",
    "bool": "boolean",
    "array": "array",
    "object": "object",
    "float": "number",
}


class ClaudeSkillAdapter:
    """插件 → Claude Skill 自动转换器。

    用法：
        adapter = ClaudeSkillAdapter(registry)
        skills = adapter.export_all()        # 全部插件转 Skill
        skill = adapter.export_one("caveman_compress")
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def export_one(self, plugin_id: str) -> dict[str, Any] | None:
        """将单个插件导出为 Claude Skill 描述。"""
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return None
        return self._manifest_to_skill(manifest)

    def export_all(self) -> list[dict[str, Any]]:
        """将全部具备 CLAUDE_SKILL 能力的插件导出为 Claude Skill 列表。"""
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            if PluginCapability.CLAUDE_SKILL not in manifest.capabilities:
                continue
            skill = self._manifest_to_skill(manifest)
            if skill is not None:
                skills.append(skill)
        return skills

    def _manifest_to_skill(
        self, manifest: PluginManifest
    ) -> dict[str, Any]:
        """将 PluginManifest 转换为 Claude Skill 描述。"""
        # 构建 input_schema（JSON Schema）
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in manifest.params:
            prop: dict[str, Any] = {
                "type": _PARAM_TYPE_MAP.get(param.type, "string"),
                "description": param.description,
            }
            if param.enum is not None:
                prop["enum"] = param.enum
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

        # Claude Skill 描述
        skill: dict[str, Any] = {
            "name": manifest.id,
            "description": manifest.description,
            "input_schema": input_schema,
            # 扩展字段，供 Desk 配置面板使用
            "_fusion": {
                "plugin_name": manifest.name,
                "version": manifest.version,
                "category": manifest.category.value,
                "capabilities": [c.value for c in manifest.capabilities],
                "default_mounted": manifest.default_mounted,
                "timeout_seconds": manifest.timeout_seconds,
            },
        }
        return skill

    def export_default_mounted(self) -> list[dict[str, Any]]:
        """导出所有 default_mounted=True 的插件 Skill（默认挂载给 Claude 会话）。

        对应 PRD「内置 caveman 等 token 压缩插件，默认挂载给 Claude 会话」。
        """
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.default_mounted():
            skill = self._manifest_to_skill(manifest)
            if skill is not None:
                skills.append(skill)
        return skills
