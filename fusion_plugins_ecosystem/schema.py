"""共享 schema 定义。

统一类型映射、MCP annotations、SandboxMode 等，
供 registry / claude_adapter / mcp_exporter / sandbox 共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PluginParamType(str, Enum):
    """插件参数类型枚举（替代自由字符串）。"""

    STRING = "string"
    INT = "int"
    BOOL = "bool"
    FLOAT = "float"
    ARRAY = "array"
    OBJECT = "object"


# JSON Schema 类型映射：PluginParamType → JSON Schema type
PARAM_TYPE_TO_JSON_SCHEMA: dict[PluginParamType, str] = {
    PluginParamType.STRING: "string",
    PluginParamType.INT: "integer",
    PluginParamType.BOOL: "boolean",
    PluginParamType.FLOAT: "number",
    PluginParamType.ARRAY: "array",
    PluginParamType.OBJECT: "object",
}

# 向后兼容：字符串 → JSON Schema type（供旧代码过渡）
_PARAM_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "int": "integer",
    "bool": "boolean",
    "float": "number",
    "array": "array",
    "object": "object",
}


class SandboxMode(str, Enum):
    """插件运行沙箱模式。"""

    INLINE = "inline"  # 进程内执行（向后兼容）
    PROCESS = "process"  # 独立子进程（需沙箱隔离）


@dataclass(frozen=True)
class MCPAnnotations:
    """MCP 工具行为注解（MCP 2026-07-28 规范）。"""

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
