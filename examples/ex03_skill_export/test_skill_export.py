"""Example 03 - tests for Skill export."""

from __future__ import annotations

import json

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.skill_adapter import SkillAdapter

from examples.ex03_skill_export.skill_plugin import FORMAT_REPORT_MANIFEST


def test_skill_bundle_frontmatter():
    registry = fpe.PluginRegistry()
    registry.register(FORMAT_REPORT_MANIFEST)
    bundle = SkillAdapter(registry).export_skill("format_report")

    assert bundle is not None
    assert bundle.skill_md.startswith("---\n")
    assert "name: format_report" in bundle.skill_md
    assert "version: 0.1.0" in bundle.skill_md
    assert "capabilities: [mcp_tool, claude_skill]" in bundle.skill_md


def test_skill_bundle_body_params_and_schema():
    registry = fpe.PluginRegistry()
    registry.register(FORMAT_REPORT_MANIFEST)
    bundle = SkillAdapter(registry).export_skill("format_report")

    assert "## Parameters" in bundle.skill_md
    assert "**title**" in bundle.skill_md
    assert "## Input Schema" in bundle.skill_md
    schema_block = bundle.skill_md.split("```json", 1)[1].split("```", 1)[0]
    schema = json.loads(schema_block)
    assert schema["type"] == "object"
    assert "title" in schema["required"]


def test_references_generated():
    registry = fpe.PluginRegistry()
    registry.register(FORMAT_REPORT_MANIFEST)
    bundle = SkillAdapter(registry).export_skill("format_report")

    assert "overview.md" in bundle.references
    assert "Format Report" in bundle.references["overview.md"]
