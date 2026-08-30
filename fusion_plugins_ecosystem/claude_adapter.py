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

    def _manifest_to_skill(self, manifest: PluginManifest) -> dict[str, Any]:
        """将 PluginManifest 转换为 Claude Skill 描述。"""
        # E7：委托 skill_adapter 的 SSOT，避免双份 input_schema 逻辑漂移
        from fusion_plugins_ecosystem.skill_adapter import build_skill_dict

        return build_skill_dict(manifest)

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
