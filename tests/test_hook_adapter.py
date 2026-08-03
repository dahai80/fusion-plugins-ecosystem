"""HookAdapter 测试。"""

from __future__ import annotations

from fusion_plugins_ecosystem.hook_adapter import HookAdapter, HookDef, HookEvent
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)


def _make_manifest(
    plugin_id: str = "test_plugin",
    capabilities: tuple[PluginCapability, ...] = (PluginCapability.MCP_TOOL,),
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="A test plugin",
        capabilities=capabilities,
        params=(),
    )


def _make_registry(*manifests: PluginManifest) -> PluginRegistry:
    reg = PluginRegistry()
    for m in manifests:
        reg.register(m)
    return reg


def test_export_hooks_mcp_tool() -> None:
    reg = _make_registry(_make_manifest("p1"))
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("p1")
    assert len(hooks) == 2
    events = [h.event for h in hooks]
    assert HookEvent.PRE_TOOL_USE in events
    assert HookEvent.POST_TOOL_USE in events


def test_export_hooks_subagent() -> None:
    reg = _make_registry(_make_manifest("p1", (PluginCapability.SUBAGENT,)))
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("p1")
    assert len(hooks) == 1
    assert hooks[0].event == HookEvent.SUBAGENT_STOP


def test_export_hooks_multiple_capabilities() -> None:
    reg = _make_registry(
        _make_manifest(
            "p1",
            (PluginCapability.MCP_TOOL, PluginCapability.LONG_TASK),
        )
    )
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("p1")
    events = [h.event for h in hooks]
    assert HookEvent.PRE_TOOL_USE in events
    assert HookEvent.POST_TOOL_USE in events
    assert HookEvent.STOP in events


def test_export_hooks_no_capability() -> None:
    reg = _make_registry(_make_manifest("p1", (PluginCapability.CLAUDE_SKILL,)))
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("p1")
    assert len(hooks) == 0


def test_export_hooks_unknown_plugin() -> None:
    reg = _make_registry()
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("unknown")
    assert len(hooks) == 0


def test_export_all() -> None:
    reg = _make_registry(
        _make_manifest("p1", (PluginCapability.MCP_TOOL,)),
        _make_manifest("p2", (PluginCapability.SUBAGENT,)),
    )
    adapter = HookAdapter(reg)
    all_hooks = adapter.export_all()
    assert len(all_hooks) == 3


def test_hook_def_to_dict() -> None:
    hook = HookDef(
        event=HookEvent.PRE_TOOL_USE,
        plugin_id="p1",
        command="fusion-plugin-server --hook p1 --event PreToolUse",
        description="PreToolUse hook for plugin Test (mcp_tool capability)",
    )
    d = hook.to_dict()
    assert d["event"] == HookEvent.PRE_TOOL_USE
    assert d["plugin_id"] == "p1"
    assert "fusion-plugin-server" in d["command"]
    assert d["description"]


def test_hook_command_format() -> None:
    reg = _make_registry(_make_manifest("my_plugin"))
    adapter = HookAdapter(reg)
    hooks = adapter.export_hooks("my_plugin")
    for hook in hooks:
        assert "my_plugin" in hook.command
        assert hook.event in hook.command
