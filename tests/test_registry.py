"""registry + claude_adapter + mcp_exporter + caveman 内置插件的集成测试。"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.builtin.caveman_compress import (
    CAVEMAN_MANIFEST,
    caveman_compress,
)
from fusion_plugins_ecosystem.registry import PluginCategory


# ── 注册中心 ──


def test_registry_register_and_get() -> None:
    registry = fpe.PluginRegistry()
    registry.register(CAVEMAN_MANIFEST)
    assert registry.get("caveman_compress") is CAVEMAN_MANIFEST


def test_registry_duplicate_same_version_idempotent() -> None:
    registry = fpe.PluginRegistry()
    registry.register(CAVEMAN_MANIFEST)
    registry.register(CAVEMAN_MANIFEST)  # 相同版本幂等


def test_registry_duplicate_different_version_raises() -> None:
    registry = fpe.PluginRegistry()
    registry.register(CAVEMAN_MANIFEST)
    different = fpe.PluginManifest(
        id=CAVEMAN_MANIFEST.id,
        name=CAVEMAN_MANIFEST.name,
        version="99.99.99",
        category=PluginCategory.CUSTOM,
        description="version conflict test",
        entry_point=lambda d, p: None,
    )
    try:
        registry.register(different)
    except ValueError as e:
        assert "版本冲突" in str(e)
        return
    raise AssertionError("不同版本重复注册应抛出 ValueError")


def test_registry_register_builtin_loads_caveman() -> None:
    registry = fpe.PluginRegistry()
    registry.register_builtin()
    assert registry.get("caveman_compress") is not None
    assert any(m.id == "caveman_compress" for m in registry.list())


def test_registry_default_mounted_includes_caveman() -> None:
    registry = fpe.PluginRegistry()
    registry.register_builtin()
    mounted = registry.default_mounted()
    assert any(m.id == "caveman_compress" for m in mounted)


# ── Claude Skill 适配 ──


def test_claude_adapter_exports_caveman_skill() -> None:
    registry = fpe.PluginRegistry()
    registry.register_builtin()
    adapter = fpe.ClaudeSkillAdapter(registry)
    skills = adapter.export_all()
    assert len(skills) == 1
    skill = skills[0]
    assert skill["name"] == "caveman_compress"
    assert "text" in skill["input_schema"]["properties"]
    assert "text" in skill["input_schema"]["required"]


def test_claude_adapter_export_default_mounted() -> None:
    registry = fpe.PluginRegistry()
    registry.register_builtin()
    adapter = fpe.ClaudeSkillAdapter(registry)
    skills = adapter.export_default_mounted()
    assert len(skills) == 1
    assert skills[0]["name"] == "caveman_compress"


# ── MCP 导出 ──


def test_mcp_exporter_lists_caveman_tool() -> None:
    registry = fpe.PluginRegistry()
    registry.register_builtin()
    exporter = fpe.MCPExporter(registry)
    tools = exporter.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "mcp__plugin__caveman_compress"
    assert "inputSchema" in tools[0]


# ── caveman 压缩功能 ──


def test_caveman_compress_removes_comments() -> None:
    text = "# 这是注释\n代码行\n// 另一种注释\n实际内容"
    result = caveman_compress(None, {"text": text})
    assert "# 这是注释" not in result["compressed"]
    assert "// 另一种注释" not in result["compressed"]
    assert "实际内容" in result["compressed"]
    assert result["original_chars"] > result["compressed_chars"]


def test_caveman_compress_collapses_blank_lines() -> None:
    text = "a\n\n\n\nb"
    result = caveman_compress(None, {"text": text})
    # 多个空行合并为一个
    assert "\n\n\n" not in result["compressed"]


def test_caveman_compress_empty_text() -> None:
    result = caveman_compress(None, {"text": ""})
    assert result["compressed"] == ""
    assert result["original_chars"] == 0
    assert result["ratio"] == 0.0


def test_caveman_compress_keep_comments() -> None:
    text = "# 注释\n内容"
    result = caveman_compress(
        None,
        {
            "text": text,
            "keep_comments": True,
        },
    )
    assert "# 注释" in result["compressed"]


# ── 配置面板 ──


def test_ecosystem_config_defaults_all_enabled() -> None:
    config = fpe.EcosystemConfig()
    assert config.enable_claude_mcp is True
    assert config.auto_export_claude_skill is True
    assert config.subagent_timeout_destroy is True
    assert config.default_mount_compressor is True


def test_ecosystem_config_roundtrip() -> None:
    config = fpe.EcosystemConfig()
    d = config.to_dict()
    restored, warnings = fpe.EcosystemConfig.from_dict(d)
    assert restored.to_dict() == d
    assert warnings == []


# ── 拓扑排序 ──


def _make_manifest(pid: str, depends_on: tuple[str, ...] = ()) -> fpe.PluginManifest:
    return fpe.PluginManifest(
        id=pid,
        name=pid,
        version="1.0.0",
        category=PluginCategory.CUSTOM,
        description=f"test {pid}",
        entry_point=lambda desk, params: None,
        depends_on=depends_on,
    )


def test_resolve_load_order_no_deps() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("a"))
    registry.register(_make_manifest("b"))
    order = registry.resolve_load_order(["b", "a"])
    assert set(order) == {"a", "b"}


def test_resolve_load_order_linear_deps() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("a"))
    registry.register(_make_manifest("b", depends_on=("a",)))
    registry.register(_make_manifest("c", depends_on=("b",)))
    order = registry.resolve_load_order(["c"])
    assert order.index("a") < order.index("b") < order.index("c")


def test_resolve_load_order_diamond_deps() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("base"))
    registry.register(_make_manifest("left", depends_on=("base",)))
    registry.register(_make_manifest("right", depends_on=("base",)))
    registry.register(_make_manifest("top", depends_on=("left", "right")))
    order = registry.resolve_load_order(["top"])
    assert order.index("base") < order.index("left")
    assert order.index("base") < order.index("right")
    assert order.index("left") < order.index("top")
    assert order.index("right") < order.index("top")


def test_resolve_load_order_cycle_raises() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("a", depends_on=("b",)))
    registry.register(_make_manifest("b", depends_on=("a",)))
    try:
        registry.resolve_load_order(["a"])
    except ValueError as e:
        assert "循环依赖" in str(e)
        return
    raise AssertionError("循环依赖应抛出 ValueError")


def test_resolve_load_order_missing_dep_raises() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("a", depends_on=("missing",)))
    try:
        registry.resolve_load_order(["a"])
    except KeyError as e:
        assert "missing" in str(e)
        return
    raise AssertionError("缺失依赖应抛出 KeyError")


def test_resolve_load_order_all_plugins() -> None:
    registry = fpe.PluginRegistry()
    registry.register(_make_manifest("a"))
    registry.register(_make_manifest("b", depends_on=("a",)))
    order = registry.resolve_load_order()
    assert order.index("a") < order.index("b")
