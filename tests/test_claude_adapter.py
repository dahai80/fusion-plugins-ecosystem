"""ClaudeSkillAdapter 测试。"""

from __future__ import annotations


from fusion_plugins_ecosystem.claude_adapter import ClaudeSkillAdapter
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)


def _make_manifest(
    plugin_id: str = "test_plugin",
    capabilities: list[PluginCapability] | None = None,
    default_mounted: bool = False,
) -> PluginManifest:
    # capabilities 显式传入时完全遵循；否则默认附加 CLAUDE_SKILL
    if capabilities is None:
        caps = [PluginCapability.CLAUDE_SKILL]
    else:
        caps = list(capabilities)
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="测试插件描述",
        capabilities=caps,
        params=[
            PluginParam(
                name="text",
                type="string",
                description="输入文本",
                required=True,
            ),
            PluginParam(
                name="count",
                type="int",
                description="数量",
                required=False,
                default=10,
            ),
            PluginParam(
                name="mode",
                type="string",
                description="模式",
                enum=["fast", "slow"],
            ),
            PluginParam(
                name="enabled",
                type="bool",
                description="是否启用",
            ),
            PluginParam(
                name="tags",
                type="array",
                description="标签列表",
            ),
            PluginParam(
                name="config",
                type="object",
                description="配置对象",
            ),
            PluginParam(
                name="score",
                type="float",
                description="分数",
            ),
        ],
        default_mounted=default_mounted,
    )


def test_export_one_returns_skill_dict() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    assert skill is not None
    assert skill["name"] == "p1"
    assert skill["description"] == "测试插件描述"
    assert skill["input_schema"]["type"] == "object"


def test_export_one_unknown_returns_none() -> None:
    registry = PluginRegistry()
    adapter = ClaudeSkillAdapter(registry)
    assert adapter.export_one("nonexistent") is None


def test_export_all_returns_all_skills() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    registry.register(_make_manifest("p2"))
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_all()
    assert len(skills) == 2
    skill_ids = {s["name"] for s in skills}
    assert skill_ids == {"p1", "p2"}


def test_export_all_filters_non_claude_skill() -> None:
    registry = PluginRegistry()
    registry.register(
        _make_manifest(
            "p1",
            capabilities=[PluginCapability.MCP_TOOL],
        )
    )
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_all()
    # 该插件没有 CLAUDE_SKILL 能力，应被过滤
    assert len(skills) == 0


def test_export_all_empty_registry() -> None:
    registry = PluginRegistry()
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_all()
    assert len(skills) == 0


def test_export_default_mounted_returns_only_mounted() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1", default_mounted=True))
    registry.register(_make_manifest("p2", default_mounted=False))
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_default_mounted()
    assert len(skills) == 1
    assert skills[0]["name"] == "p1"


def test_export_default_mounted_none_mounted() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1", default_mounted=False))
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_default_mounted()
    assert len(skills) == 0


def test_skill_includes_all_param_types() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    props = skill["input_schema"]["properties"]
    assert props["text"]["type"] == "string"
    assert props["count"]["type"] == "integer"
    assert props["mode"]["type"] == "string"
    assert props["enabled"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"
    assert props["config"]["type"] == "object"
    assert props["score"]["type"] == "number"


def test_skill_includes_enum_and_default() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    props = skill["input_schema"]["properties"]
    assert props["mode"]["enum"] == ["fast", "slow"]
    assert props["count"]["default"] == 10


def test_skill_required_field() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    assert "text" in skill["input_schema"]["required"]
    assert "count" not in skill["input_schema"]["required"]


def test_skill_required_field_empty_when_none_required() -> None:
    m = PluginManifest(
        id="p1",
        name="Test",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="desc",
        capabilities=[PluginCapability.CLAUDE_SKILL],
        params=[PluginParam(name="x", type="string", description="d")],
    )
    registry = PluginRegistry()
    registry.register(m)
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    assert "required" not in skill["input_schema"]


def test_skill_fusion_metadata_included() -> None:
    registry = PluginRegistry()
    registry.register(_make_manifest("p1"))
    adapter = ClaudeSkillAdapter(registry)
    skill = adapter.export_one("p1")
    assert "_fusion" in skill
    assert skill["_fusion"]["plugin_name"] == "Test Plugin"
    assert skill["_fusion"]["version"] == "0.1.0"
    assert skill["_fusion"]["category"] == "custom"
    assert PluginCapability.CLAUDE_SKILL.value in skill["_fusion"]["capabilities"]


def test_skill_with_caveman_builtin() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    adapter = ClaudeSkillAdapter(registry)
    skills = adapter.export_all()
    assert len(skills) == 1
    assert skills[0]["name"] == "caveman_compress"
