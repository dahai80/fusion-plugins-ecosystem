"""PluginRegistry 测试。"""

from __future__ import annotations

import pytest

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)


def _make_manifest(plugin_id: str = "p1") -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        capabilities=[PluginCapability.MCP_TOOL],
        params=[PluginParam(name="x", type="string", description="d", required=True)],
    )


def test_register_and_get() -> None:
    registry = PluginRegistry()
    m = _make_manifest("p1")
    registry.register(m)
    assert registry.get("p1") is m


def test_register_adds_to_desk_registered_ids() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    assert "p1" in registry.desk.registered_plugin_ids


def test_register_duplicate_raises() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    with pytest.raises(ValueError, match="已注册"):
        registry.register(_make_manifest("p1"))


def test_unregister_removes() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.unregister("p1")
    assert registry.get("p1") is None
    assert "p1" not in registry.desk.registered_plugin_ids


def test_unregister_unknown_noop() -> None:
    registry = PluginRegistry()
    registry.unregister("unknown")  # 不应抛异常


def test_get_unknown_returns_none() -> None:
    registry = PluginRegistry()
    assert registry.get("unknown") is None


def test_list_all() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p2"))
    plugins = registry.list()
    assert len(plugins) == 2


def test_list_by_category() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    m2 = _make_manifest("p2")
    m2.category = PluginCategory.MLX_INFERENCE
    registry.register(m2)
    mlx_plugins = registry.list(PluginCategory.MLX_INFERENCE)
    assert len(mlx_plugins) == 1
    assert mlx_plugins[0].id == "p2"


def test_list_empty() -> None:
    registry = PluginRegistry()
    assert registry.list() == []


def test_register_builtin_loads_caveman() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    assert registry.get("caveman_compress") is not None
    assert any(m.id == "caveman_compress" for m in registry.list())


def test_default_mounted_returns_only_mounted() -> None:
    registry = PluginRegistry()
    m1 = _make_manifest("p1")
    m1.default_mounted = True
    m2 = _make_manifest("p2")
    m2.default_mounted = False
    registry.register(m1)
    registry.register(m2)
    mounted = registry.default_mounted()
    assert len(mounted) == 1
    assert mounted[0].id == "p1"


def test_default_mounted_empty() -> None:
    registry = PluginRegistry()
    assert registry.default_mounted() == []


def test_category_enum_values() -> None:
    assert PluginCategory.CODING_PLAN.value == "coding_plan"
    assert PluginCategory.CONTEXT_COMPRESS.value == "context_compress"
    assert PluginCategory.MLX_INFERENCE.value == "mlx_inference"
    assert PluginCategory.TERMINAL_PROXY.value == "terminal_proxy"
    assert PluginCategory.FILE_INDEX.value == "file_index"
    assert PluginCategory.QUANTIZATION.value == "quantization"
    assert PluginCategory.VISUAL_BACKEND.value == "visual_backend"
    assert PluginCategory.CUSTOM.value == "custom"


def test_capability_enum_values() -> None:
    assert PluginCapability.MCP_TOOL.value == "mcp_tool"
    assert PluginCapability.CLAUDE_SKILL.value == "claude_skill"
    assert PluginCapability.SUBAGENT.value == "subagent"
    assert PluginCapability.FILE_ACCESS.value == "file_access"
    assert PluginCapability.VRAM_CONSUMER.value == "vram_consumer"
    assert PluginCapability.LONG_TASK.value == "long_task"


def test_plugin_param_defaults() -> None:
    p = PluginParam(name="x", type="string", description="d")
    assert p.required is False
    assert p.default is None
    assert p.enum is None


def test_plugin_manifest_defaults() -> None:
    m = PluginManifest(
        id="p1",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
    )
    assert m.capabilities == []
    assert m.params == []
    assert m.entry_point is None
    assert m.default_mounted is False
    assert m.timeout_seconds is None
    assert m.vram_mb == 0
