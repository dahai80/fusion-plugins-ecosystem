# Claude full-chain unified gateway.
#
# Centralized compatibility layer that consolidates the PRD's three layers:
# - Claude Desktop: connected via MCP protocol (stdio / SSE)
# - Claude Code (VS Code): subagent dispatch + long tasks + tool calls
# - MCP protocol: fusion-cowork exposes an MCP Server, plugin capabilities
#   are auto-registered as MCP Tools
#
# Covers the PRD's bidirectional interop:
# - Forward: Claude calls all local fusion capabilities (MLX inference,
#   file operations, quantization tools)
# - Reverse: fusion-cowork proactively spawns Claude Code subagents for
#   batch refactoring and PR generation
#
# Plus unified Volcengine Ark Claude Coding Plan credential management.
#
# Design principles:
# - ClaudeGateway is a thin orchestrator; real capability is delegated to:
#   · ClaudeSkillAdapter (plugin → Skill)
#   · MCPExporter (plugin → MCP Tools)
#   · PluginLifecycle (subagent execution + timeout meltdown + auto-restart)
#   · DeskRuntime (MLX inference, file permissions, API keys)
# - All switches are driven by EcosystemConfig, all on by default
#   (fully compatible out of the box)

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.mcp_exporter import MCPExporter
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginRegistry,
)
from fusion_plugins_ecosystem.skill_adapter import SkillAdapter
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter

logger = logging.getLogger(__name__)


# Claude client types (per PRD "support web Claude, Claude Desktop client,
# and the VS Code Claude Code plugin as the three client types")
CLAUDE_DESKTOP = "claude_desktop"  # Claude Desktop client
CLAUDE_CODE = "claude_code"  # VS Code Claude Code plugin
CLAUDE_WEB = "claude_web"  # Web Claude
CLAUDE_VOLCENGINE = "claude_volcengine"  # Volcengine Ark Claude Coding Plan


@dataclass
class SubagentTask:
    # Claude Code subagent task description (reverse interop).

    name: str  # task name (e.g. "batch-refactor")
    plugin_id: str  # plugin to execute
    arguments: dict[str, Any]  # plugin input arguments
    timeout_seconds: int | None = None  # timeout (None inherits from config)
    metadata: dict[str, Any] = field(default_factory=dict)


class ClaudeGateway:
    # Claude full-chain unified gateway.
    #
    # Usage:
    #     gateway = ClaudeGateway(registry, lifecycle, desk, config)
    #     # Forward: expose to Claude
    #     skills = gateway.export_skills()                # Claude Skill
    #     mcp_tools = gateway.list_mcp_tools()            # MCP Tools
    #     mcp_response = await gateway.invoke_mcp_tool(...)  # MCP tools/call
    #     # Reverse: spawn a Claude Code subagent
    #     result = await gateway.dispatch_subagent(task)
    #     # Credentials
    #     gateway.store_credentials("volcengine_claude", "sk-xxx")
    #     key = gateway.get_credentials("volcengine_claude")

    def __init__(
        self,
        registry: PluginRegistry,
        lifecycle: PluginLifecycle | None = None,
        desk: DeskRuntime | None = None,
        config: EcosystemConfig | None = None,
        token_meter: TokenMeter | None = None,
    ) -> None:
        self.registry = registry
        self.desk: DeskRuntime = desk or registry.desk
        self.config: EcosystemConfig = config or EcosystemConfig()
        # P3-1: inject config into lifecycle so _maybe_restart's exponential
        # backoff takes effect (with config=None the backoff degrades to 0,
        # i.e. restarts with no delay).
        self.lifecycle: PluginLifecycle = lifecycle or PluginLifecycle(
            registry, config=self.config
        )
        self.token_meter: TokenMeter = token_meter or TokenMeter(
            self.desk,
            max_records=self.config.max_token_records,
            persist_path=self.config.token_persist_path,
        )
        # Delegate adapters
        self._skill_adapter = SkillAdapter(registry)
        self._mcp_exporter = MCPExporter(registry, self.desk)

    # ── Forward: Claude calls fusion capabilities ──

    def export_skills(self) -> list[dict[str, Any]]:
        # Export all plugins as a Claude Skill list.
        # Gated by config.auto_export_claude_skill.
        if not self.config.auto_export_claude_skill:
            logger.info("claude_gateway: Skill auto-export is disabled")
            return []
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            skill = self._skill_adapter.export_one(manifest.id)
            if skill is not None:
                skills.append(skill)
        return skills

    def export_default_mounted_skills(self) -> list[dict[str, Any]]:
        # Export default-mounted plugin Skills (per PRD "mount by default
        # into Claude sessions"). Gated by config.default_mount_compressor.
        if not self.config.default_mount_compressor:
            return []
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.default_mounted():
            skill = self._skill_adapter.export_one(manifest.id)
            if skill is not None:
                skills.append(skill)
        return skills

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        # List MCP Tools (for Claude Desktop / Claude Code integration).
        # Gated by config.enable_claude_mcp.
        if not self.config.enable_claude_mcp:
            logger.info("claude_gateway: MCP exposure is disabled")
            return []
        return self._mcp_exporter.list_tools()

    async def invoke_mcp_tool(
        self,
        plugin_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        # MCP tools/call forwarding: Claude calls a fusion plugin via MCP.
        # Gated by config.enable_claude_mcp; returns isError=True when off.
        if not self.config.enable_claude_mcp:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "MCP exposure is disabled",
                    }
                ],
                "isError": True,
            }
        # Verify the plugin has the MCP_TOOL capability
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return self._mcp_error(plugin_id, f"plugin {plugin_id!r} is not registered")
        if PluginCapability.MCP_TOOL not in manifest.capabilities:
            return self._mcp_error(
                plugin_id,
                f"plugin {plugin_id!r} does not have the MCP_TOOL capability",
            )
        # Delegate to lifecycle execution (with timeout meltdown + auto-restart)
        try:
            await self.lifecycle.enable(plugin_id)
            with self.token_meter.measure(
                plugin_id,
                TokenKind.MCP_RELAY,
                metadata={"plugin": plugin_id, "arguments": arguments},
            ):
                result = await self.lifecycle.execute(plugin_id, arguments)
            return self._mcp_success(plugin_id, result)
        except Exception as exc:
            # P1-7: raw exception detail goes only to desk.log; a generic
            # message is returned externally to prevent information leakage
            self.desk.log(
                plugin_id,
                "ERROR",
                "MCP tools/call execution failed",
                error=str(exc),
            )
            logger.error(
                "claude_gateway: invoke_mcp_tool %s error: %s",
                plugin_id,
                exc,
                exc_info=True,
            )
            return self._mcp_error(plugin_id, "execution failed")

    def gateway_info(self) -> dict[str, Any]:
        # Return MCP gateway metadata (for Claude Desktop / Claude Code setup).
        info = self._mcp_exporter.gateway_info()
        info["skills_count"] = len(self.export_skills())
        info["default_mounted_count"] = len(self.export_default_mounted_skills())
        info["config"] = self.config.to_dict()
        return info

    # ── Reverse: fusion-cowork spawns Claude Code subagents ──

    async def dispatch_subagent(self, task: SubagentTask) -> dict[str, Any]:
        # Spawn a Claude Code subagent to execute a task (reverse interop).
        #
        # Per PRD "fusion-cowork can proactively spawn Claude Code subagents
        # for batch refactoring, PR generation, and code optimization".
        #
        # Controlled by config.subagent_timeout_destroy: auto-destroy on
        # timeout.
        manifest = self.registry.get(task.plugin_id)
        if manifest is None:
            raise KeyError(
                f"plugin {task.plugin_id!r} of subagent task {task.name!r} is not registered"
            )
        # Resolve timeout: task > manifest > config
        timeout = (
            task.timeout_seconds
            or manifest.timeout_seconds
            or self.config.subagent_timeout_seconds
        )
        try:
            await self.lifecycle.enable(task.plugin_id)
            model_hint = manifest.agent_model or ""
            with self.token_meter.measure(
                task.plugin_id,
                TokenKind.PLUGIN_LOCAL,
                metadata={
                    "subagent": task.name,
                    "metadata": task.metadata,
                    "agent_model": model_hint,
                },
            ):
                result = await self.lifecycle.execute(
                    task.plugin_id,
                    task.arguments,
                    timeout_override=timeout,
                )
            return {
                "task": task.name,
                "plugin_id": task.plugin_id,
                "state": "completed",
                "result": result,
            }
        except asyncio.TimeoutError as exc:
            self.desk.log(
                task.plugin_id,
                "ERROR",
                "subagent task timed out",
                task=task.name,
                error=str(exc),
            )
            # Destroy on timeout only: the field name subagent_timeout_destroy
            # means "destroy after timeout"; crashes follow the preserve path
            # for troubleshooting (P2-5: the old implementation unloaded on
            # every exception, the opposite of the field's naming).
            if self.config.subagent_timeout_destroy:
                self.lifecycle.unload(task.plugin_id)
                self.desk.log(
                    task.plugin_id,
                    "WARN",
                    "subagent timed out, destroyed automatically",
                    task=task.name,
                )
            return {
                "task": task.name,
                "plugin_id": task.plugin_id,
                "state": "failed",
                "error": str(exc),
            }
        except Exception as exc:
            self.desk.log(
                task.plugin_id,
                "ERROR",
                "subagent task crashed",
                task=task.name,
                error=str(exc),
            )
            # Preserve the instance on crash (CRASHED state) for
            # troubleshooting; destroy is triggered only by the timeout branch
            return {
                "task": task.name,
                "plugin_id": task.plugin_id,
                "state": "failed",
                "error": str(exc),
            }

    def list_subagent_capable_plugins(self) -> list[str]:
        # List plugin IDs with the SUBAGENT capability (for the Claude Code
        # subagent dispatch panel).
        return [
            m.id
            for m in self.registry.list()
            if PluginCapability.SUBAGENT in m.capabilities
        ]

    # ── Volcengine Ark Claude Coding Plan credentials ──

    def store_credentials(self, provider: str, api_key: str) -> None:
        # Store a Claude API key in the Desk credential center.
        #
        # Supports Volcengine Ark Claude Coding Plan credentials:
        # provider="volcengine_claude" stores the Volcengine Ark API key.
        #
        # Controlled by config.enable_volcengine_claude_plan (Volcengine
        # provider only).
        if (
            provider == "volcengine_claude"
            and not self.config.enable_volcengine_claude_plan
        ):
            self.desk.log(
                "claude_gateway",
                "WARN",
                "Volcengine Ark Claude Coding Plan auth is disabled, key not stored",
                provider=provider,
            )
            return
        self.desk.set_api_key(provider, api_key)
        self.desk.log(
            "claude_gateway",
            "INFO",
            "Claude API key stored",
            provider=provider,
        )

    def get_credentials(self, provider: str) -> str | None:
        # Read a Claude API key.
        # Controlled by config.enable_volcengine_claude_plan (Volcengine
        # provider only).
        if (
            provider == "volcengine_claude"
            and not self.config.enable_volcengine_claude_plan
        ):
            return None
        return self.desk.get_api_key(provider)

    def has_credentials(self, provider: str) -> bool:
        # Check whether a key is configured for the given provider.
        return self.get_credentials(provider) is not None

    # ── fusion-mlx as Claude vision/image-generation backend ──

    async def mlx_visual_backend(
        self,
        model: str,
        messages: list[dict],
        **kwargs: Any,
    ) -> Any:
        # Call fusion-mlx local inference as the Claude vision/image
        # generation backend. Controlled by config.enable_mixed_quantization.
        if not self.config.enable_mixed_quantization:
            raise RuntimeError("mixed quantization + MLX backend is disabled")
        with self.token_meter.measure(
            "mlx_visual_backend",
            TokenKind.MLX_INFERENCE,
            metadata={"model": model},
        ):
            return await self.desk.mlx_chat(model, messages, **kwargs)

    # ── Internal helpers ──

    @staticmethod
    def _mcp_success(plugin_id: str, result: Any) -> dict[str, Any]:
        # Build a successful MCP tools/call response.
        # Serialize dict results to text content; other types via str()
        if isinstance(result, dict):
            text = json.dumps(result, ensure_ascii=False, default=str)
        else:
            text = str(result)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }

    @staticmethod
    def _mcp_error(plugin_id: str, message: str) -> dict[str, Any]:
        # Build an error MCP tools/call response.
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[plugin={plugin_id}] {message}",
                }
            ],
            "isError": True,
        }
