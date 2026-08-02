"""schema.py 单元测试。"""

from __future__ import annotations

import pytest

from fusion_plugins_ecosystem.schema import (
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSIONS_SUPPORTED,
    MCPAnnotations,
    PARAM_TYPE_TO_JSON_SCHEMA,
    PluginParamType,
    SandboxMode,
    _PARAM_TYPE_MAP,
)


# ── PluginParamType ──


def test_param_type_is_str_enum() -> None:
    assert isinstance(PluginParamType.STRING, str)
    assert PluginParamType.STRING == "string"


def test_param_type_all_values() -> None:
    expected = {"STRING", "INT", "BOOL", "FLOAT", "ARRAY", "OBJECT"}
    actual = {e.name for e in PluginParamType}
    assert actual == expected


def test_param_type_value_matches_json_schema() -> None:
    assert PluginParamType.STRING.value == "string"
    assert PluginParamType.INT.value == "int"
    assert PluginParamType.BOOL.value == "bool"
    assert PluginParamType.FLOAT.value == "float"
    assert PluginParamType.ARRAY.value == "array"
    assert PluginParamType.OBJECT.value == "object"


# ── PARAM_TYPE_TO_JSON_SCHEMA ──


def test_json_schema_map_completeness() -> None:
    for pt in PluginParamType:
        assert pt in PARAM_TYPE_TO_JSON_SCHEMA


def test_json_schema_map_values() -> None:
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.STRING] == "string"
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.INT] == "integer"
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.BOOL] == "boolean"
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.FLOAT] == "number"
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.ARRAY] == "array"
    assert PARAM_TYPE_TO_JSON_SCHEMA[PluginParamType.OBJECT] == "object"


# ── _PARAM_TYPE_MAP (backward compat) ──


def test_param_type_map_string_keys() -> None:
    assert _PARAM_TYPE_MAP["string"] == "string"
    assert _PARAM_TYPE_MAP["int"] == "integer"
    assert _PARAM_TYPE_MAP["bool"] == "boolean"
    assert _PARAM_TYPE_MAP["float"] == "number"
    assert _PARAM_TYPE_MAP["array"] == "array"
    assert _PARAM_TYPE_MAP["object"] == "object"


def test_param_type_map_unknown_key_returns_none() -> None:
    assert _PARAM_TYPE_MAP.get("unknown") is None


# ── SandboxMode ──


def test_sandbox_mode_values() -> None:
    assert SandboxMode.INLINE == "inline"
    assert SandboxMode.PROCESS == "process"


def test_sandbox_mode_is_str_enum() -> None:
    assert isinstance(SandboxMode.INLINE, str)


# ── MCPAnnotations ──


def test_mcp_annotations_defaults() -> None:
    ann = MCPAnnotations()
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False
    assert ann.idempotentHint is False
    assert ann.openWorldHint is True


def test_mcp_annotations_custom() -> None:
    ann = MCPAnnotations(
        readOnlyHint=True,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is True
    assert ann.idempotentHint is True
    assert ann.openWorldHint is False


def test_mcp_annotations_frozen() -> None:
    ann = MCPAnnotations()
    with pytest.raises(AttributeError):
        ann.readOnlyHint = True  # type: ignore[misc]


def test_mcp_annotations_to_dict() -> None:
    ann = MCPAnnotations(readOnlyHint=True, openWorldHint=False)
    d = ann.to_dict()
    assert d == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }


def test_mcp_annotations_to_dict_all_default() -> None:
    ann = MCPAnnotations()
    d = ann.to_dict()
    assert d == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }


# ── Protocol version constants ──


def test_protocol_version_current() -> None:
    assert MCP_PROTOCOL_VERSION == "2026-07-28"


def test_protocol_versions_supported_contains_current() -> None:
    assert MCP_PROTOCOL_VERSION in MCP_PROTOCOL_VERSIONS_SUPPORTED


def test_protocol_versions_supported_contains_legacy() -> None:
    assert "2024-11-05" in MCP_PROTOCOL_VERSIONS_SUPPORTED


def test_protocol_versions_supported_is_list() -> None:
    assert isinstance(MCP_PROTOCOL_VERSIONS_SUPPORTED, list)
    assert len(MCP_PROTOCOL_VERSIONS_SUPPORTED) >= 2
