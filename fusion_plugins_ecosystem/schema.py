# Shared schema definitions.
# Unified type mapping, MCP annotations, SandboxMode, etc., shared by
# registry / claude_adapter / mcp_exporter / sandbox.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PluginParamType(str, Enum):
    # Plugin parameter type enum (replaces free-form strings).
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    FLOAT = "float"
    ARRAY = "array"
    OBJECT = "object"


# JSON Schema type mapping: PluginParamType → JSON Schema type
PARAM_TYPE_TO_JSON_SCHEMA: dict[PluginParamType, str] = {
    PluginParamType.STRING: "string",
    PluginParamType.INT: "integer",
    PluginParamType.BOOL: "boolean",
    PluginParamType.FLOAT: "number",
    PluginParamType.ARRAY: "array",
    PluginParamType.OBJECT: "object",
}

# Backward compat: string → JSON Schema type (transition for older code)
_PARAM_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "int": "integer",
    "bool": "boolean",
    "float": "number",
    "array": "array",
    "object": "object",
}


class SandboxMode(str, Enum):
    # Plugin execution sandbox mode.
    INLINE = "inline"  # in-process execution (backward compat)
    PROCESS = "process"  # separate subprocess (requires sandbox isolation)


@dataclass(frozen=True)
class MCPAnnotations:
    # MCP tool behavior annotations (MCP 2026-07-28 spec).

    readOnlyHint: bool = False
    destructiveHint: bool = False
    idempotentHint: bool = False
    openWorldHint: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.readOnlyHint,
            "destructiveHint": self.destructiveHint,
            "idempotentHint": self.idempotentHint,
            "openWorldHint": self.openWorldHint,
        }


MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSIONS_SUPPORTED = [
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
]
