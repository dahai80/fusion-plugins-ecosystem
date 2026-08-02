"""registry + claude_adapter + mcp_exporter + caveman 内置插件的集成测试。"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.builtin.caveman_compress import (
    CAVEMAN_MANIFEST,
    caveman_compress,
)


# ── 注册中心 ──


def test_registry_register_and_get() -> None:
    registry = fpe.PluginRegistry()
    registry.register(CAVEMAN_MANIFEST)
    assert registry.get("caveman_compress") is CAVEMAN_MANIFEST


def test_registry_duplicate_raises() -> None:
    registry = fpe.PluginRegistry()
    registry.register(CAVEMAN_MANIFEST)
    try:
        registry.register(CAVEMAN_MANIFEST)
    except ValueError:
        return
    raise AssertionError("重复注册应抛出 ValueError")


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
    assert tools[0]["name"] == "caveman_compress"
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
