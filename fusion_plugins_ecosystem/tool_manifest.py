"""Declarative tool-manifest interchange format (issue #68).

One static, lintable JSON file per plugin:

    {
      "identifier": "caveman_compress",
      "meta": {"name": ..., "description": ..., "icon": ..., "version": ...},
      "api": [{"name": ..., "description": ..., "parameters": {json-schema}}],
      "systemRole": "system-prompt fragment injected when the tool is enabled"
    }

Deterministic converters map manifests <-> MCP tool definitions
(`tools/list` output), so builtin tools export to MCP clients and MCP
servers surface through the same registry. Validation is build/startup
time (jsonschema Draft 2020-12) — invalid manifests fail loudly, never
at model runtime.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_VERSION = "1.0"

# JSON-Schema (Draft 2020-12) for the manifest file itself. Shipped as the
# single validation source: parse_tool_manifest() runs it via jsonschema.
TOOL_MANIFEST_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "FusionToolManifest",
    "type": "object",
    "required": ["identifier", "meta", "api", "systemRole"],
    "additionalProperties": False,
    "properties": {
        "identifier": {"type": "string", "minLength": 1, "pattern": "^[a-zA-Z0-9_-]+$"},
        "meta": {
            "type": "object",
            "required": ["name", "description", "version"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "icon": {"type": "string"},
                "version": {"type": "string", "minLength": 1},
            },
        },
        "api": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "description", "parameters"],
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "^[a-zA-Z0-9_-]+$",
                    },
                    "description": {"type": "string"},
                    "parameters": {"type": "object"},
                },
            },
        },
        "systemRole": {"type": "string", "minLength": 1},
    },
}


class ToolManifestError(ValueError):
    """Invalid tool manifest — raised at load/startup time, fails loudly."""


@dataclass(frozen=True)
class ToolApiSpec:
    """One callable endpoint declared as JSON-Schema parameters."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolManifest:
    """Static per-plugin tool manifest (issue #68 interchange format)."""

    identifier: str
    name: str
    description: str
    version: str
    api: tuple[ToolApiSpec, ...]
    system_role: str
    icon: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "identifier": self.identifier,
            "meta": {
                "name": self.name,
                "description": self.description,
                "version": self.version,
            },
            "api": [
                {
                    "name": a.name,
                    "description": a.description,
                    "parameters": a.parameters,
                }
                for a in self.api
            ],
            "systemRole": self.system_role,
        }
        if self.icon:
            out["meta"]["icon"] = self.icon
        return out


def _validate_json_schema_object(schema: Any, where: str) -> None:
    """Parameters must themselves be a valid JSON-Schema object."""
    import jsonschema

    if not isinstance(schema, dict):
        raise ToolManifestError(f"{where}: parameters must be a JSON-Schema object")
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ToolManifestError(f"{where}: invalid JSON-Schema parameters: {e}") from e


def parse_tool_manifest(data: Any) -> ToolManifest:
    """Validate + parse a manifest dict. Raises ToolManifestError on any defect."""
    import jsonschema

    if not isinstance(data, dict):
        raise ToolManifestError("manifest must be a JSON object")
    try:
        jsonschema.validate(data, TOOL_MANIFEST_JSON_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ToolManifestError(
            f"manifest schema violation at {list(e.absolute_path)}: {e.message}"
        ) from e
    names = [a["name"] for a in data["api"]]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ToolManifestError(f"duplicate api names: {dupes}")
    for i, a in enumerate(data["api"]):
        _validate_json_schema_object(a["parameters"], f"api[{i}].{a['name']}")
    meta = data["meta"]
    api = tuple(
        ToolApiSpec(
            name=a["name"], description=a["description"], parameters=a["parameters"]
        )
        for a in data["api"]
    )
    return ToolManifest(
        identifier=data["identifier"],
        name=meta["name"],
        description=meta["description"],
        version=meta["version"],
        api=api,
        system_role=data["systemRole"],
        icon=meta.get("icon", ""),
    )


def load_tool_manifest(path: str | Path) -> ToolManifest:
    """Load + validate a manifest JSON file."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ToolManifestError(f"cannot read manifest {p}: {e}") from e
    return parse_tool_manifest(data)


def manifest_to_mcp(manifest: ToolManifest) -> list[dict[str, Any]]:
    """Manifest -> MCP `tools/list` entries (deterministic, name-mangled
    `<identifier>__<api>` to stay unique across plugins)."""
    return [
        {
            "name": f"{manifest.identifier}__{a.name}",
            "description": a.description or manifest.description,
            "inputSchema": dict(a.parameters),
        }
        for a in manifest.api
    ]


def manifest_from_mcp(
    identifier: str,
    name: str,
    description: str,
    version: str,
    tools: list[dict[str, Any]],
    system_role: str = "",
    icon: str = "",
) -> ToolManifest:
    """MCP `tools/list` output -> ToolManifest. Inverse of manifest_to_mcp:
    strips the `<identifier>__` name prefix; missing systemRole defaults to
    the tool description echo (explicit system_role preferred)."""
    api: list[ToolApiSpec] = []
    prefix = f"{identifier}__"
    for i, t in enumerate(tools):
        tool_name = str(t.get("name") or "")
        if not tool_name:
            raise ToolManifestError(f"tools[{i}]: missing name")
        short = tool_name[len(prefix) :] if tool_name.startswith(prefix) else tool_name
        schema = t.get("inputSchema")
        _validate_json_schema_object(schema, f"tools[{i}].{tool_name}")
        api.append(
            ToolApiSpec(
                name=short,
                description=str(t.get("description") or ""),
                parameters=dict(schema),
            )
        )
    return parse_tool_manifest(
        {
            "identifier": identifier,
            "meta": {
                "name": name,
                "description": description,
                **({"icon": icon} if icon else {}),
                "version": version,
            },
            "api": [
                {
                    "name": a.name,
                    "description": a.description,
                    "parameters": a.parameters,
                }
                for a in api
            ],
            "systemRole": system_role or description,
        }
    )
