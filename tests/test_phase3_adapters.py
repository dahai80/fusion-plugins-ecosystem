"""SkillAdapter / AgentAdapter / PluginBundleGenerator 测试。"""

from __future__ import annotations

import json


from fusion_plugins_ecosystem.agent_adapter import AgentAdapter
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.plugin_bundle import PluginBundle, PluginBundleGenerator
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginParamType,
    PluginRegistry,
)
from fusion_plugins_ecosystem.skill_adapter import SkillAdapter, SkillBundle


def _make_registry(*manifests: PluginManifest) -> PluginRegistry:
    reg = PluginRegistry(desk=DeskRuntime())
    for m in manifests:
        reg.register(m)
    return reg


def _make_manifest(
    plugin_id: str = "test_plugin",
    name: str = "Test Plugin",
    capabilities: tuple[PluginCapability, ...] = (PluginCapability.MCP_TOOL,),
    category: PluginCategory = PluginCategory.CONTEXT_COMPRESS,
    params: tuple[PluginParam, ...] = (),
    default_mounted: bool = False,
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name=name,
        version="0.1.0",
        category=category,
        description="A test plugin",
        capabilities=capabilities,
        params=params,
        default_mounted=default_mounted,
    )


# ── SkillAdapter ──


class TestSkillAdapter:
    def test_export_skill_returns_bundle(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        adapter = SkillAdapter(reg)
        bundle = adapter.export_skill("p1")
        assert bundle is not None
        assert isinstance(bundle, SkillBundle)
        assert "---\n" in bundle.skill_md
        assert "name: p1" in bundle.skill_md

    def test_export_skill_not_found(self) -> None:
        reg = _make_registry()
        adapter = SkillAdapter(reg)
        assert adapter.export_skill("missing") is None

    def test_export_skill_with_params(self) -> None:
        params = (
            PluginParam(
                name="threshold",
                type=PluginParamType.FLOAT,
                description="Compression threshold",
                required=True,
            ),
            PluginParam(
                name="mode",
                type=PluginParamType.STRING,
                description="Mode",
                enum=("fast", "slow"),
                default="fast",
            ),
        )
        reg = _make_registry(_make_manifest("comp", params=params))
        adapter = SkillAdapter(reg)
        bundle = adapter.export_skill("comp")
        assert bundle is not None
        assert "threshold" in bundle.skill_md
        assert "mode" in bundle.skill_md
        assert "## Input Schema" in bundle.skill_md

    def test_export_all_filters_claude_skill_only(self) -> None:
        m1 = _make_manifest("p1", capabilities=(PluginCapability.CLAUDE_SKILL,))
        m2 = _make_manifest("p2", capabilities=(PluginCapability.FILE_ACCESS,))
        reg = _make_registry(m1, m2)
        adapter = SkillAdapter(reg)
        bundles = adapter.export_all()
        assert len(bundles) == 1
        assert bundles[0].skill_md.startswith("---\n")

    def test_export_default_mounted(self) -> None:
        m1 = _make_manifest("p1", default_mounted=True)
        m2 = _make_manifest("p2", default_mounted=False)
        reg = _make_registry(m1, m2)
        adapter = SkillAdapter(reg)
        bundles = adapter.export_default_mounted()
        assert len(bundles) == 1

    def test_skill_md_frontmatter(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        adapter = SkillAdapter(reg)
        bundle = adapter.export_skill("p1")
        assert bundle is not None
        lines = bundle.skill_md.split("\n")
        assert lines[0] == "---"
        assert any("name: p1" in ln for ln in lines)
        assert any("version: 0.1.0" in ln for ln in lines)

    def test_skill_references_generated(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        adapter = SkillAdapter(reg)
        bundle = adapter.export_skill("p1")
        assert "overview.md" in bundle.references

    def test_export_one_backward_compat(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        adapter = SkillAdapter(reg)
        skill_dict = adapter.export_one("p1")
        assert skill_dict is not None
        assert skill_dict["name"] == "p1"
        assert "input_schema" in skill_dict
        assert "_fusion" in skill_dict

    def test_export_one_not_found(self) -> None:
        reg = _make_registry()
        adapter = SkillAdapter(reg)
        assert adapter.export_one("missing") is None

    def test_escape_yaml(self) -> None:
        assert SkillAdapter._escape_yaml("simple") == "simple"
        assert ":" in SkillAdapter._escape_yaml("has: colon")
        assert "#" in SkillAdapter._escape_yaml("has# hash")


# ── AgentAdapter ──


class TestAgentAdapter:
    def test_export_agent_subagent_capability(self) -> None:
        m = _make_manifest(
            "agent1",
            capabilities=(PluginCapability.SUBAGENT, PluginCapability.MCP_TOOL),
        )
        reg = _make_registry(m)
        adapter = AgentAdapter(reg)
        md = adapter.export_agent("agent1")
        assert md is not None
        assert "---\n" in md
        assert "name: agent1" in md
        assert "model: inherit" in md

    def test_export_agent_no_subagent_capability(self) -> None:
        m = _make_manifest("tool1", capabilities=(PluginCapability.MCP_TOOL,))
        reg = _make_registry(m)
        adapter = AgentAdapter(reg)
        assert adapter.export_agent("tool1") is None

    def test_export_agent_not_found(self) -> None:
        reg = _make_registry()
        adapter = AgentAdapter(reg)
        assert adapter.export_agent("missing") is None

    def test_export_all(self) -> None:
        m1 = _make_manifest(
            "a1",
            capabilities=(PluginCapability.SUBAGENT,),
        )
        m2 = _make_manifest(
            "a2",
            capabilities=(PluginCapability.MCP_TOOL,),
        )
        reg = _make_registry(m1, m2)
        adapter = AgentAdapter(reg)
        agents = adapter.export_all()
        assert len(agents) == 1
        assert "a1" in agents[0]

    def test_agent_frontmatter_color(self) -> None:
        m = _make_manifest(
            "viz",
            capabilities=(PluginCapability.SUBAGENT,),
            category=PluginCategory.VISUAL_BACKEND,
        )
        reg = _make_registry(m)
        adapter = AgentAdapter(reg)
        md = adapter.export_agent("viz")
        assert md is not None
        assert "#9B59B6" in md

    def test_agent_with_params(self) -> None:
        params = (
            PluginParam(
                name="model",
                type=PluginParamType.STRING,
                description="Model name",
                required=True,
            ),
        )
        m = _make_manifest(
            "p1",
            capabilities=(PluginCapability.SUBAGENT,),
            params=params,
        )
        reg = _make_registry(m)
        adapter = AgentAdapter(reg)
        md = adapter.export_agent("p1")
        assert md is not None
        assert "model" in md
        assert "## Configuration" in md


# ── PluginBundleGenerator ──


class TestPluginBundleGenerator:
    def test_generate_basic(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        assert isinstance(bundle, PluginBundle)
        assert bundle.plugin_id == "p1"
        assert bundle.plugin_json != ""

    def test_generate_not_found(self) -> None:
        reg = _make_registry()
        gen = PluginBundleGenerator(reg)
        assert gen.generate("missing") is None

    def test_plugin_json_content(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        data = json.loads(bundle.plugin_json)
        assert data["name"] == "p1"
        assert data["version"] == "0.1.0"
        assert "mcp_tool" in data["capabilities"]

    def test_mcp_config_generated_for_mcp_tool(self) -> None:
        reg = _make_registry(
            _make_manifest("p1", capabilities=(PluginCapability.MCP_TOOL,))
        )
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        assert bundle.mcp_config != ""
        mcp = json.loads(bundle.mcp_config)
        assert "mcpServers" in mcp
        assert "p1" in mcp["mcpServers"]

    def test_mcp_config_empty_for_no_mcp(self) -> None:
        m = _make_manifest("p1", capabilities=(PluginCapability.FILE_ACCESS,))
        reg = _make_registry(m)
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        assert bundle.mcp_config == ""

    def test_skills_included(self) -> None:
        reg = _make_registry(_make_manifest("p1"))
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        assert "p1" in bundle.skills

    def test_agents_included_for_subagent(self) -> None:
        m = _make_manifest(
            "a1",
            capabilities=(PluginCapability.SUBAGENT, PluginCapability.MCP_TOOL),
        )
        reg = _make_registry(m)
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("a1")
        assert bundle is not None
        assert len(bundle.agents) == 1

    def test_generate_all(self) -> None:
        m1 = _make_manifest("p1")
        m2 = _make_manifest("p2")
        reg = _make_registry(m1, m2)
        gen = PluginBundleGenerator(reg)
        bundles = gen.generate_all()
        assert len(bundles) == 2
        ids = {b.plugin_id for b in bundles}
        assert ids == {"p1", "p2"}

    def test_plugin_json_with_params(self) -> None:
        params = (
            PluginParam(
                name="ratio",
                type=PluginParamType.FLOAT,
                description="Ratio",
                required=False,
                default=0.5,
            ),
        )
        reg = _make_registry(_make_manifest("p1", params=params))
        gen = PluginBundleGenerator(reg)
        bundle = gen.generate("p1")
        assert bundle is not None
        data = json.loads(bundle.plugin_json)
        assert "params" in data
        assert data["params"][0]["name"] == "ratio"
