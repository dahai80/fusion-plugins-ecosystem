"""Tool-manifest interchange format tests (issue #68).

Covers: schema validation (valid/invalid), JSON-Schema parameter checks,
MCP round-trip on representative tools, builtin startup validation
(fail loudly on invalid manifests).
"""

import json
from pathlib import Path

import pytest

from fusion_plugins_ecosystem import __version__
from fusion_plugins_ecosystem.registry import PluginRegistry
from fusion_plugins_ecosystem.tool_manifest import (
    TOOL_MANIFEST_JSON_SCHEMA,
    ToolManifestError,
    load_tool_manifest,
    manifest_from_mcp,
    manifest_to_mcp,
    parse_tool_manifest,
)

BUILTIN_MANIFEST = (
    Path(__file__).parent.parent
    / "fusion_plugins_ecosystem"
    / "builtin"
    / "caveman_compress.tool.json"
)


def _sample(**over) -> dict:
    data = {
        "identifier": "bank_api",
        "meta": {
            "name": "Bank API",
            "description": "Bank reconciliation tools",
            "version": "1.0.0",
        },
        "api": [
            {
                "name": "fetch_flow",
                "description": "Fetch transaction flow",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "required": ["account_id"],
                },
            },
            {
                "name": "push_order",
                "description": "Push an order (per-use confirm)",
                "parameters": {
                    "type": "object",
                    "properties": {"amount": {"type": "number"}},
                    "required": ["amount"],
                },
            },
        ],
        "systemRole": "Handle bank data read-only unless per-use confirmed.",
    }
    data.update(over)
    return data


class TestParse:
    def test_valid_manifest_parses(self):
        m = parse_tool_manifest(_sample())
        assert m.identifier == "bank_api"
        assert m.name == "Bank API" and m.version == "1.0.0"
        assert [a.name for a in m.api] == ["fetch_flow", "push_order"]
        assert m.api[0].parameters["required"] == ["account_id"]
        assert "read-only" in m.system_role
        assert m.to_dict()["meta"]["name"] == "Bank API"

    def test_missing_identifier_rejected(self):
        data = _sample()
        del data["identifier"]
        with pytest.raises(ToolManifestError):
            parse_tool_manifest(data)

    def test_bad_identifier_pattern_rejected(self):
        with pytest.raises(ToolManifestError):
            parse_tool_manifest(_sample(identifier="bad id!"))

    def test_empty_api_rejected(self):
        with pytest.raises(ToolManifestError):
            parse_tool_manifest(_sample(api=[]))

    def test_duplicate_api_names_rejected(self):
        data = _sample()
        data["api"].append(dict(data["api"][0]))
        with pytest.raises(ToolManifestError, match="duplicate"):
            parse_tool_manifest(data)

    def test_invalid_parameter_json_schema_rejected(self):
        data = _sample()
        data["api"][0]["parameters"] = {"type": 123}
        with pytest.raises(ToolManifestError, match="invalid JSON-Schema"):
            parse_tool_manifest(data)

    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(ToolManifestError):
            parse_tool_manifest(_sample(extra="x"))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ToolManifestError, match="cannot read"):
            load_tool_manifest(tmp_path / "nope.json")

    def test_shipped_json_schema_is_draft2020(self):
        assert TOOL_MANIFEST_JSON_SCHEMA["$schema"].endswith("draft/2020-12/schema")


class TestMcpRoundTrip:
    def test_manifest_to_mcp_shape(self):
        m = parse_tool_manifest(_sample())
        tools = manifest_to_mcp(m)
        assert [t["name"] for t in tools] == [
            "bank_api__fetch_flow",
            "bank_api__push_order",
        ]
        assert tools[0]["inputSchema"]["type"] == "object"
        assert tools[0]["inputSchema"]["required"] == ["account_id"]

    def test_round_trip_property_on_representative_tools(self):
        # manifest -> MCP tools/list -> manifest must be identity
        for src in (_sample(), json.loads(BUILTIN_MANIFEST.read_text())):
            m = parse_tool_manifest(src)
            back = manifest_from_mcp(
                identifier=m.identifier,
                name=m.name,
                description=m.description,
                version=m.version,
                tools=manifest_to_mcp(m),
                system_role=m.system_role,
                icon=m.icon,
            )
            assert back == m

    def test_from_mcp_accepts_unprefixed_names(self):
        m = manifest_from_mcp(
            identifier="srv",
            name="Srv",
            description="d",
            version="0.1.0",
            tools=[
                {
                    "name": "ping",
                    "description": "Ping",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ],
        )
        assert m.api[0].name == "ping"
        assert m.system_role == "d"

    def test_from_mcp_rejects_missing_name(self):
        with pytest.raises(ToolManifestError, match="missing name"):
            manifest_from_mcp(
                "srv",
                "S",
                "d",
                "0.1.0",
                tools=[{"inputSchema": {"type": "object"}}],
            )


class TestBuiltinStartup:
    def test_builtin_manifest_file_valid_and_version_matches_package(self):
        m = load_tool_manifest(BUILTIN_MANIFEST)
        assert m.identifier == "caveman_compress"
        assert m.version == __version__
        assert any(a.name == "compress" for a in m.api)

    def test_register_builtin_loads_tool_manifest(self):
        registry = PluginRegistry()
        registry.register_builtin()
        tm = registry.tool_manifests.get("caveman_compress")
        assert tm is not None and tm.identifier == "caveman_compress"

    def test_invalid_builtin_manifest_fails_loudly(self, tmp_path, monkeypatch):
        # Corrupt file on disk -> register_builtin must raise, not swallow
        bad = tmp_path / "caveman_compress.tool.json"
        bad.write_text(json.dumps({"identifier": "x"}), encoding="utf-8")
        import fusion_plugins_ecosystem.tool_manifest as tm_mod

        real_load = tm_mod.load_tool_manifest
        monkeypatch.setattr(
            tm_mod,
            "load_tool_manifest",
            lambda p: real_load(bad),
        )
        registry = PluginRegistry()
        with pytest.raises(ToolManifestError):
            registry.register_builtin()
