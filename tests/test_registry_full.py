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


def test_register_duplicate_same_version_idempotent() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p1"))  # 相同版本幂等


def test_register_duplicate_different_version_raises() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    m2 = PluginManifest(
        id="p1",
        name="Test",
        version="2.0.0",
        category=PluginCategory.CUSTOM,
        description="conflict",
        entry_point=lambda d, p: None,
    )
    with pytest.raises(ValueError, match="版本冲突"):
        registry.register(m2)


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
    m2 = PluginManifest(
        id="p2",
        name="Test",
        version="0.1.0",
        category=PluginCategory.MLX_INFERENCE,
        description="d",
    )
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
    m1 = PluginManifest(
        id="p1",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        default_mounted=True,
    )
    m2 = PluginManifest(
        id="p2",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        default_mounted=False,
    )
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
    assert m.capabilities == ()
    assert m.params == ()
    assert m.entry_point is None
    assert m.default_mounted is False
    assert m.timeout_seconds is None
    assert m.vram_mb == 0


def test_plugin_param_to_dict() -> None:
    p = PluginParam(
        name="x",
        type="string",
        description="test param",
        required=True,
        default="hello",
        enum=("a", "b"),
    )
    d = p.to_dict()
    assert d["name"] == "x"
    assert d["type"] == "string"
    assert d["required"] is True
    assert d["default"] == "hello"
    assert d["enum"] == ["a", "b"]


def test_plugin_param_to_dict_no_enum() -> None:
    p = PluginParam(name="y", type="int", description="d")
    d = p.to_dict()
    assert d["enum"] is None


def test_manifest_to_dict() -> None:
    m = _make_manifest("p1")
    d = m.to_dict()
    assert d["id"] == "p1"
    assert d["name"] == "Test"
    assert d["category"] == "custom"
    assert d["capabilities"] == ["mcp_tool"]
    assert len(d["params"]) == 1
    assert d["params"][0]["name"] == "x"
    assert d["sandbox_mode"] == "inline"
    assert d["depends_on"] == []
    assert d["vram_mb"] == 0


def test_manifest_to_dict_callable_entry_point() -> None:
    def my_entry(desk, params):
        return {}

    m = PluginManifest(
        id="p1",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        entry_point=my_entry,
    )
    d = m.to_dict()
    assert ":" in d["entry_point"]
    assert "my_entry" in d["entry_point"]


def test_manifest_to_dict_string_entry_point() -> None:
    m = PluginManifest(
        id="p1",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        entry_point="mymodule:main",
    )
    d = m.to_dict()
    assert d["entry_point"] == "mymodule:main"


def test_list_as_dicts() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p2"))
    result = registry.list_as_dicts()
    assert len(result) == 2
    assert all(isinstance(r, dict) for r in result)
    ids = {r["id"] for r in result}
    assert ids == {"p1", "p2"}


def test_list_as_dicts_with_category_filter() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(
        PluginManifest(
            id="p2",
            name="MLX",
            version="0.1.0",
            category=PluginCategory.MLX_INFERENCE,
            description="d",
        )
    )
    result = registry.list_as_dicts(category=PluginCategory.MLX_INFERENCE)
    assert len(result) == 1
    assert result[0]["category"] == "mlx_inference"


# ── CLD-04: agent_model ──


def test_manifest_agent_model_default_none() -> None:
    m = _make_manifest("p1")
    assert m.agent_model is None


def test_manifest_agent_model_in_to_dict() -> None:
    m = PluginManifest(
        id="p1",
        name="Agent",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="d",
        entry_point=lambda d, p: None,
        agent_model="claude-sonnet-5",
    )
    d = m.to_dict()
    assert d["agent_model"] == "claude-sonnet-5"
