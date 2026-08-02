"""Claude Code Hook 适配器。

将插件能力映射为 Claude Code Plugin hooks/ 目录下的事件处理器定义，
支持 PreToolUse / PostToolUse / Notification 等事件类型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
)

logger = logging.getLogger(__name__)


class HookEvent(str):
    """Claude Code 支持的 Hook 事件类型。"""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    NOTIFICATION = "Notification"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"


_CAPABILITY_HOOK_EVENTS: dict[PluginCapability, list[str]] = {
    PluginCapability.MCP_TOOL: [HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE],
    PluginCapability.SUBAGENT: [HookEvent.SUBAGENT_STOP],
    PluginCapability.LONG_TASK: [HookEvent.STOP],
    PluginCapability.VRAM_CONSUMER: [HookEvent.NOTIFICATION],
    PluginCapability.FILE_ACCESS: [HookEvent.PRE_TOOL_USE],
}


@dataclass(frozen=True)
class HookDef:
    """单个 Hook 事件定义。"""

    event: str
    plugin_id: str
    command: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "event": self.event,
            "plugin_id": self.plugin_id,
            "command": self.command,
            "description": self.description,
        }


class HookAdapter:
    """插件 → Claude Code Hook 事件导出器。

    用法：
        adapter = HookAdapter(registry)
        hooks = adapter.export_hooks("my_plugin")
        all_hooks = adapter.export_all()
    """

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def export_hooks(self, plugin_id: str) -> list[HookDef]:
        manifest = self._registry.get(plugin_id)
        if manifest is None:
            logger.warning("hook_adapter: plugin %s not found", plugin_id)
            return []
        return self._build_hooks(manifest)

    def export_all(self) -> list[HookDef]:
        hooks: list[HookDef] = []
        for manifest in self._registry.list():
            hooks.extend(self._build_hooks(manifest))
        return hooks

    def _build_hooks(self, manifest: PluginManifest) -> list[HookDef]:
        hooks: list[HookDef] = []
        for cap in manifest.capabilities:
            events = _CAPABILITY_HOOK_EVENTS.get(cap, [])
            for event in events:
                hooks.append(
                    HookDef(
                        event=event,
                        plugin_id=manifest.id,
                        command=f"fusion-plugin-server --hook {manifest.id} --event {event}",
                        description=(
                            f"{event} hook for plugin {manifest.name} "
                            f"({cap.value} capability)"
                        ),
                    )
                )
        return hooks
