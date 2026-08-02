"""Claude 全链路统一网关。

集中式兼容层，把 PRD 的三层打通集中到一处：
- Claude Desktop：通过 MCP 协议（stdio / SSE）对接
- Claude Code (VS Code)：子代理调度 + 长任务 + 工具调用
- MCP 协议：fusion-desk 暴露 MCP Server，插件能力自动注册为 MCP Tools

并覆盖 PRD 的双向互通：
- 正向：Claude 调用 fusion 全部本地能力（MLX 推理、文件操作、量化工具）
- 反向：fusion-desk 主动拉起 Claude Code 子代理，完成项目批量重构、PR 生成

以及火山方舟 Claude Coding Plan 套餐鉴权统一管理。

设计原则：
- ClaudeGateway 是 thin orchestrator，真实能力委托给：
  · ClaudeSkillAdapter（插件 → Skill）
  · MCPExporter（插件 → MCP Tools）
  · PluginLifecycle（子代理执行 + 超时熔断 + 自动重启）
  · DeskContext（MLX 推理、文件权限、API 密钥）
- 所有开关由 EcosystemConfig 驱动，默认全开（原生完整兼容）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fusion_plugins_ecosystem.skill_adapter import SkillAdapter
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
from fusion_plugins_ecosystem.mcp_exporter import MCPExporter
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginRegistry,
)
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter

logger = logging.getLogger(__name__)


# Claude 接入方式（对应 PRD「支持网页版 Claude、Claude Desktop 客户端、
# VS Code Claude Code 插件三种接入方式」）
CLAUDE_DESKTOP = "claude_desktop"        # Claude Desktop 客户端
CLAUDE_CODE = "claude_code"              # VS Code Claude Code 插件
CLAUDE_WEB = "claude_web"                 # 网页版 Claude
CLAUDE_VOLCENGINE = "claude_volcengine"  # 火山方舟 Claude Coding Plan


@dataclass
class SubagentTask:
    """Claude Code 子代理任务描述（反向互通）。"""

    name: str                          # 任务名称（如 "batch-refactor"）
    plugin_id: str                     # 执行插件 ID
    arguments: dict[str, Any]          # 插件入参
    timeout_seconds: int | None = None  # 超时（None 继承 config）
    metadata: dict[str, Any] = field(default_factory=dict)


class ClaudeGateway:
    """Claude 全链路统一网关。

    用法：
        gateway = ClaudeGateway(registry, lifecycle, desk, config)
        # 正向：暴露给 Claude
        skills = gateway.export_skills()                # Claude Skill
        mcp_tools = gateway.list_mcp_tools()            # MCP Tools
        mcp_response = await gateway.invoke_mcp_tool(...)  # MCP tools/call
        # 反向：拉起 Claude Code 子代理
        result = await gateway.dispatch_subagent(task)
        # 鉴权
        gateway.store_credentials("volcengine_claude", "sk-xxx")
        key = gateway.get_credentials("volcengine_claude")
    """

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
        self.lifecycle: PluginLifecycle = lifecycle or PluginLifecycle(
            registry
        )
        self.config: EcosystemConfig = config or EcosystemConfig()
        self.token_meter: TokenMeter = token_meter or TokenMeter(self.desk)
        # 委托适配器
        self._skill_adapter = SkillAdapter(registry)
        self._mcp_exporter = MCPExporter(registry, self.desk)

    # ── 正向：Claude 调用 fusion 能力 ──

    def export_skills(self) -> list[dict[str, Any]]:
        """导出全部插件为 Claude Skill 列表。

        受 config.auto_export_claude_skill 开关控制。
        """
        if not self.config.auto_export_claude_skill:
            logger.info("claude_gateway: Skill 自动导出已关闭")
            return []
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.list():
            skill = self._skill_adapter.export_one(manifest.id)
            if skill is not None:
                skills.append(skill)
        return skills

    def export_default_mounted_skills(self) -> list[dict[str, Any]]:
        """导出默认挂载插件 Skill（对应 PRD「默认挂载给 Claude 会话」）。

        受 config.default_mount_compressor 开关控制。
        """
        if not self.config.default_mount_compressor:
            return []
        skills: list[dict[str, Any]] = []
        for manifest in self.registry.default_mounted():
            skill = self._skill_adapter.export_one(manifest.id)
            if skill is not None:
                skills.append(skill)
        return skills

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        """列出 MCP Tools（供给 Claude Desktop / Claude Code 对接）。

        受 config.enable_claude_mcp 开关控制。
        """
        if not self.config.enable_claude_mcp:
            logger.info("claude_gateway: MCP 对外暴露已关闭")
            return []
        return self._mcp_exporter.list_tools()

    async def invoke_mcp_tool(
        self,
        plugin_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """MCP tools/call 转发：Claude 通过 MCP 调用 fusion 插件。

        受 config.enable_claude_mcp 开关控制；关闭时返回 isError=True。
        """
        if not self.config.enable_claude_mcp:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "MCP 对外暴露已关闭",
                    }
                ],
                "isError": True,
            }
        # 校验插件是否具备 MCP_TOOL 能力
        manifest = self.registry.get(plugin_id)
        if manifest is None:
            return self._mcp_error(
                plugin_id, f"插件 {plugin_id!r} 未注册"
            )
        if PluginCapability.MCP_TOOL not in manifest.capabilities:
            return self._mcp_error(
                plugin_id,
                f"插件 {plugin_id!r} 不具备 MCP_TOOL 能力",
            )
        # 委托 lifecycle 执行（带超时熔断 + 自动重启）
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
            self.desk.log(
                plugin_id,
                "ERROR",
                "MCP tools/call 执行失败",
                error=str(exc),
            )
            return self._mcp_error(plugin_id, f"执行失败: {exc}")

    def gateway_info(self) -> dict[str, Any]:
        """返回 MCP 网关元信息（供 Claude Desktop / Claude Code 配置对接）。"""
        info = self._mcp_exporter.gateway_info()
        info["skills_count"] = len(self.export_skills())
        info["default_mounted_count"] = len(
            self.export_default_mounted_skills()
        )
        info["config"] = self.config.to_dict()
        return info

    # ── 反向：fusion-desk 拉起 Claude Code 子代理 ──

    async def dispatch_subagent(
        self, task: SubagentTask
    ) -> dict[str, Any]:
        """拉起 Claude Code 子代理执行任务（反向互通）。

        对应 PRD「fusion-desk 可主动拉起 Claude Code 子代理，完成项目
       批量重构、PR 生成、代码优化」。

        受 config.subagent_timeout_destroy 控制：超时后自动销毁。
        """
        manifest = self.registry.get(task.plugin_id)
        if manifest is None:
            raise KeyError(
                f"子代理任务 {task.name!r} 的插件 {task.plugin_id!r} 未注册"
            )
        # 计算 timeout：task > manifest > config
        timeout = (
            task.timeout_seconds
            or manifest.timeout_seconds
            or self.config.subagent_timeout_seconds
        )
        try:
            await self.lifecycle.enable(task.plugin_id)
            with self.token_meter.measure(
                task.plugin_id,
                TokenKind.PLUGIN_LOCAL,
                metadata={
                    "subagent": task.name,
                    "metadata": task.metadata,
                },
            ):
                result = await self.lifecycle.execute(
                    task.plugin_id, task.arguments,
                    timeout_override=timeout,
                )
            return {
                "task": task.name,
                "plugin_id": task.plugin_id,
                "state": "completed",
                "result": result,
            }
        except Exception as exc:
            self.desk.log(
                task.plugin_id,
                "ERROR",
                "子代理任务执行失败",
                task=task.name,
                error=str(exc),
            )
            # 超时自动销毁
            if self.config.subagent_timeout_destroy:
                self.lifecycle.unload(task.plugin_id)
                self.desk.log(
                    task.plugin_id,
                    "WARN",
                    "子代理超时/崩溃，已自动销毁",
                    task=task.name,
                )
            return {
                "task": task.name,
                "plugin_id": task.plugin_id,
                "state": "failed",
                "error": str(exc),
            }

    def list_subagent_capable_plugins(self) -> list[str]:
        """列出具备 SUBAGENT 能力的插件 ID（供 Claude Code 子代理调度面板）。"""
        return [
            m.id
            for m in self.registry.list()
            if PluginCapability.SUBAGENT in m.capabilities
        ]

    # ── 火山方舟 Claude Coding Plan 鉴权 ──

    def store_credentials(
        self, provider: str, api_key: str
    ) -> None:
        """存储 Claude API 密钥到 Desk 配置中心。

        兼容火山方舟 Claude Coding Plan 套餐鉴权：
        provider="volcengine_claude" 存火山方舟 API key。

        受 config.enable_volcengine_claude_plan 控制（仅对火山方舟 provider）。
        """
        if (
            provider == "volcengine_claude"
            and not self.config.enable_volcengine_claude_plan
        ):
            self.desk.log(
                "claude_gateway",
                "WARN",
                "火山方舟 Claude Coding Plan 鉴权已关闭，密钥未存储",
                provider=provider,
            )
            return
        self.desk.set_api_key(provider, api_key)
        self.desk.log(
            "claude_gateway",
            "INFO",
            "Claude API 密钥已存储",
            provider=provider,
        )

    def get_credentials(self, provider: str) -> str | None:
        """读取 Claude API 密钥。

        受 config.enable_volcengine_claude_plan 控制（仅对火山方舟 provider）。
        """
        if (
            provider == "volcengine_claude"
            and not self.config.enable_volcengine_claude_plan
        ):
            return None
        return self.desk.get_api_key(provider)

    def has_credentials(self, provider: str) -> bool:
        """检查是否已配置指定 provider 的密钥。"""
        return self.get_credentials(provider) is not None

    # ── fusion-mlx 作为 Claude 视觉/图像生成后端 ──

    async def mlx_visual_backend(
        self,
        model: str,
        messages: list[dict],
        **kwargs: Any,
    ) -> Any:
        """调用 fusion-mlx 本地推理作为 Claude 视觉/图像生成后端。

        受 config.enable_mixed_quantization 控制。
        """
        if not self.config.enable_mixed_quantization:
            raise RuntimeError("混合量化 + MLX 后端已关闭")
        with self.token_meter.measure(
            "mlx_visual_backend",
            TokenKind.MLX_INFERENCE,
            metadata={"model": model},
        ):
            return await self.desk.mlx_chat(model, messages, **kwargs)

    # ── 内部辅助 ──

    @staticmethod
    def _mcp_success(plugin_id: str, result: Any) -> dict[str, Any]:
        """构造 MCP tools/call 成功响应。"""
        # 将 dict 结果序列化为 text content；其他类型直接 str()
        if isinstance(result, dict):
            import json

            text = json.dumps(result, ensure_ascii=False, default=str)
        else:
            text = str(result)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }

    @staticmethod
    def _mcp_error(plugin_id: str, message: str) -> dict[str, Any]:
        """构造 MCP tools/call 错误响应。"""
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[plugin={plugin_id}] {message}",
                }
            ],
            "isError": True,
        }
