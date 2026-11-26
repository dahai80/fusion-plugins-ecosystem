# Plugin registry.
#
# Standardizes plugin metadata, parameter schemas, and capability
# declarations. All ecosystem plugins (caveman compression, quantization
# tools, code analysis, local file search) register through this center.
#
# Design principles:
# - The registry holds no plugin instances, only manifests (declarative)
# - Instantiation is done on demand by PluginLifecycle for easy hot reload
# - The manifest also drives automatic Claude Skill generation

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode

logger = logging.getLogger(__name__)


def _version_key(version: str) -> tuple:
    # Parse a semantic version string into a comparable tuple (E5).
    #
    # "1.2.0" -> (1, 2, 0); non-numeric segments are kept as strings.
    # On parse failure it falls back to the original segments, guaranteeing
    # no exception and preserving the existing "different version = reject"
    # semantics.
    parts = []
    for seg in str(version).split("."):
        if seg.isdigit():
            parts.append(int(seg))
        else:
            parts.append(seg)
    return tuple(parts)


class PluginCategory(str, Enum):
    # Plugin categories (per PRD "Claude Code exclusive plugin categories").

    CODING_PLAN = "coding_plan"  # coding-plan accelerator plugins
    CONTEXT_COMPRESS = "context_compress"  # code context compression (caveman etc.)
    MLX_INFERENCE = "mlx_inference"  # local MLX model inference plugins
    TERMINAL_PROXY = "terminal_proxy"  # terminal command proxy plugins
    FILE_INDEX = "file_index"  # local file search
    QUANTIZATION = "quantization"  # mixed quantization tools
    VISUAL_BACKEND = "visual_backend"  # Claude vision/image generation backend
    CONNECTOR = "connector"  # external MCP connector tools (connector pack bridge)
    CUSTOM = "custom"


class PluginCapability(str, Enum):
    # Plugin capability declarations, drive MCP Tools / Claude Skill
    # auto-exposure.

    MCP_TOOL = "mcp_tool"  # exposed as an MCP Tool
    CLAUDE_SKILL = "claude_skill"  # auto-converted to a Claude Skill
    SUBAGENT = "subagent"  # Claude Code subagent
    FILE_ACCESS = "file_access"  # local file read/write
    VRAM_CONSUMER = "vram_consumer"  # consumes vRAM, needs Desk scheduling
    LONG_TASK = "long_task"  # long task, needs timeout meltdown


@dataclass(frozen=True)
class PluginParam:
    # Plugin parameter schema (for Claude Skill parameter descriptions +
    # the Desk config panel).

    name: str
    type: PluginParamType | str  # PluginParamType enum, str kept for backward compat
    description: str
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": str(self.type),
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "enum": list(self.enum) if self.enum else None,
        }


@dataclass(frozen=True)
class PluginManifest:
    # Plugin manifest (declarative, no instances, immutable).

    id: str  # globally unique, e.g. "caveman_compress"
    name: str  # user-friendly name
    version: str
    category: PluginCategory
    description: str
    capabilities: tuple[PluginCapability, ...] = ()
    params: tuple[PluginParam, ...] = ()
    # Plugin entry point (callable or dotted path)
    entry_point: Callable[..., Any] | str | None = None
    # Whether it is mounted to Claude sessions by default (per PRD "builtin
    # caveman mounted by default")
    default_mounted: bool = False
    # Long-task timeout in seconds (None inherits the Desk default)
    timeout_seconds: int | None = None
    # Estimated vRAM usage (MB), 0 = none
    vram_mb: int = 0
    # Other plugin IDs this one depends on (loaded in topological order)
    depends_on: tuple[str, ...] = ()
    # Sandbox run mode
    sandbox_mode: SandboxMode = SandboxMode.INLINE
    # Plugin-level max restart count (None inherits the global MAX_RESTART)
    max_restart: int | None = None
    # MCP tool output JSON Schema (MCP 2026-07-28 outputSchema)
    output_schema: dict[str, Any] | None = None
    # MCP tool behavior annotations
    mcp_annotations: Any = None
    # Claude model used by the subagent (None = default model)
    agent_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        ep = self.entry_point
        if callable(ep):
            ep = f"{ep.__module__}:{ep.__qualname__}"
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category.value,
            "description": self.description,
            "capabilities": [c.value for c in self.capabilities],
            "params": [p.to_dict() for p in self.params],
            "entry_point": ep,
            "default_mounted": self.default_mounted,
            "timeout_seconds": self.timeout_seconds,
            "vram_mb": self.vram_mb,
            "depends_on": list(self.depends_on),
            "sandbox_mode": self.sandbox_mode.value,
            "max_restart": self.max_restart,
            "output_schema": self.output_schema,
            "mcp_annotations": self.mcp_annotations.to_dict() if self.mcp_annotations else None,
            "agent_model": self.agent_model,
        }


class PluginRegistry:
    # Plugin registry.
    #
    # Usage:
    #     registry = PluginRegistry(desk)
    #     registry.register(manifest)
    #     registry.register_builtin()    # register builtin caveman etc.

    def __init__(self, desk: DeskRuntime | None = None) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self.tool_manifests: dict[str, Any] = {}
        self.desk: DeskRuntime = desk or DeskRuntime()

    def register(self, manifest: PluginManifest) -> None:
        # Register a plugin manifest. Same ID + same version is idempotent;
        # a different version is rejected.
        existing = self._manifests.get(manifest.id)
        if existing is not None:
            # E5: semantic version comparison, "1.0" and "1.0.0" are
            # equivalent (idempotently ignored)
            if _version_key(existing.version) == _version_key(manifest.version):
                logger.debug(
                    "registry: plugin %s v%s re-registered (idempotent, ignored)",
                    manifest.id,
                    manifest.version,
                )
                return
            raise ValueError(
                f"plugin {manifest.id!r} version conflict: registered v{existing.version}, got v{manifest.version}"
            )
        self._manifests[manifest.id] = manifest
        self.desk.registered_plugin_ids.add(manifest.id)
        logger.info("registry: plugin %s v%s registered", manifest.id, manifest.version)

    def unregister(self, plugin_id: str) -> None:
        # Unregister a plugin.
        if plugin_id in self._manifests:
            del self._manifests[plugin_id]
            self.desk.registered_plugin_ids.discard(plugin_id)
            logger.info("registry: plugin %s unregistered", plugin_id)

    def get(self, plugin_id: str) -> PluginManifest | None:
        # Get a plugin manifest.
        return self._manifests.get(plugin_id)

    def list(self, category: PluginCategory | None = None) -> list[PluginManifest]:
        # List plugins (optionally filtered by category).
        result = list(self._manifests.values())
        if category is not None:
            result = [m for m in result if m.category == category]
        return result

    def list_as_dicts(self, category: PluginCategory | None = None) -> list[dict[str, Any]]:
        # List plugins as JSON-friendly dicts (for Swift/Kotlin/TS consumers).
        return [m.to_dict() for m in self.list(category=category)]

    def resolve_load_order(self, plugin_ids: list[str] | None = None) -> list[str]:
        # Return the load order sorted by dependency topology.
        #
        # Args:
        #     plugin_ids: plugin IDs to load; None means all.
        #
        # Returns:
        #     Sorted plugin ID list (dependencies first).
        #
        # Raises:
        #     ValueError: circular dependency.
        #     KeyError: a dependent plugin is not registered.
        targets = set(plugin_ids) if plugin_ids else set(self._manifests.keys())
        visited: set[str] = set()
        order: list[str] = []
        in_stack: set[str] = set()

        def visit(pid: str) -> None:
            if pid in visited:
                return
            if pid in in_stack:
                raise ValueError(f"circular dependency: {' → '.join(in_stack)} → {pid}")
            manifest = self._manifests.get(pid)
            if manifest is None:
                raise KeyError(f"dependent plugin {pid!r} is not registered")
            in_stack.add(pid)
            for dep in manifest.depends_on:
                visit(dep)
            in_stack.discard(pid)
            visited.add(pid)
            order.append(pid)

        for pid in sorted(targets):
            visit(pid)
        return order

    def register_builtin(self) -> None:
        # Register builtin plugins (explicit aggregate list).
        #
        # Append new builtin plugins here. No longer uses pkgutil+dir()
        # scanning: dir() picks up *_MANIFEST symbols imported into the
        # module namespace (accidentally registering non-builtin plugins),
        # the order is unstable, and newly added modules cannot be
        # statically discovered at import time (P2-6).
        from fusion_plugins_ecosystem.builtin.caveman_compress import (
            CAVEMAN_MANIFEST,
        )
        from fusion_plugins_ecosystem.tool_manifest import load_tool_manifest

        builtin_manifests: list[PluginManifest] = [
            CAVEMAN_MANIFEST,
        ]
        for manifest in builtin_manifests:
            self.register(manifest)
        # issue #68: static tool manifests validate at startup — fail loudly
        self.tool_manifests["caveman_compress"] = load_tool_manifest(
            Path(__file__).parent / "builtin" / "caveman_compress.tool.json"
        )

    def default_mounted(self) -> list[PluginManifest]:
        # Return all plugins with default_mounted=True (mounted to Claude
        # sessions by default).
        return [m for m in self._manifests.values() if m.default_mounted]
