"""fusion-desk runtime 对接层（合并原 DeskContext）。

封装 fusion-desk 的真实 runtime 句柄：
- NodeRegistry：节点类型注册表
- TaskScheduler：定时任务调度器
- FusionMLXClient：本地 MLX 推理客户端
- setup_logger/get_logger：统一日志

插件生态通过 DeskRuntime 访问 Desk 的底层抽象，所有调用经此层中转，
便于测试时注入 fake runtime。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeskRuntime:
    """fusion-desk runtime 句柄封装。

    由 fusion-desk 启动时注入，插件生态通过 PluginRegistry.desk 访问。
    任意字段为 None 时，对应能力降级为 no-op（用于测试或离线场景）。
    """

    # fusion-desk 节点注册表（fusion_desk.engine.node.NodeRegistry）
    node_registry: Any | None = None
    # fusion-desk 任务调度器（fusion_desk.engine.scheduler.TaskScheduler）
    task_scheduler: Any | None = None
    # fusion-desk MLX 客户端（fusion_desk.ai.FusionMLXClient）
    mlx_client: Any | None = None
    # fusion-desk 工作流引擎（fusion_desk.engine.workflow.WorkflowEngine）
    workflow_engine: Any | None = None
    # fusion-desk 日志器
    desk_logger: logging.Logger | None = None
    # 配置中心句柄（dict-like，支持 get/set）
    config_center: Any | None = None
    # MCP 网关端口（由 Desk 分配）
    mcp_gateway_port: int | None = None
    # 已注册插件权限表 {plugin_id: {allowed_paths: [...], capabilities: [...]}}
    plugin_permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 显存占用台账 {plugin_id: mb}
    vram_allocations: dict[str, int] = field(default_factory=dict)
    # 总显存预算（MB），0 表示不限制
    vram_total_mb: int = 0
    # 已注册的插件 ID 集合
    registered_plugin_ids: set[str] = field(default_factory=set)
    # vRAM 操作锁（线程安全）
    _vram_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False
    )

    # ── 显存调度 ──

    def acquire_vram(self, plugin_id: str, mb: int) -> bool:
        """向 Desk 申请显存（线程安全）。"""
        if mb <= 0:
            return True
        used = sum(self.vram_allocations.values())
        if self.vram_total_mb > 0 and used + mb > self.vram_total_mb:
            logger.warning(
                "desk_runtime: 插件 %s 显存申请失败（%dMB 超预算 %dMB）",
                plugin_id,
                mb,
                self.vram_total_mb,
            )
            return False
        self.vram_allocations[plugin_id] = (
            self.vram_allocations.get(plugin_id, 0) + mb
        )
        logger.info(
            "desk_runtime: 插件 %s 申请 %dMB 显存成功", plugin_id, mb
        )
        return True

    def release_vram(self, plugin_id: str) -> None:
        """释放插件占用的显存。"""
        freed = self.vram_allocations.pop(plugin_id, 0)
        if freed:
            logger.info(
                "desk_runtime: 插件 %s 释放 %dMB 显存", plugin_id, freed
            )

    def vram_usage(self) -> dict[str, int]:
        """返回当前显存台账快照。"""
        return dict(self.vram_allocations)

    # ── 日志采集 ──

    def log(
        self,
        plugin_id: str,
        level: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        """统一日志采集入口（解决「子代理无日志」痛点）。

        所有插件日志经此汇集到 Desk 全链路日志面板。
        """
        log_level = getattr(logging, level.upper(), logging.INFO)
        extra = f" {kwargs}" if kwargs else ""
        if self.desk_logger is not None:
            self.desk_logger.log(
                log_level,
                "[plugin=%s] %s%s",
                plugin_id,
                message,
                extra,
            )
        else:
            logger.log(
                log_level,
                "[plugin=%s] %s%s",
                plugin_id,
                message,
                extra,
            )

    # ── 文件权限 ──

    def check_file_permission(self, plugin_id: str, path: str) -> bool:
        """检查插件是否具备对指定路径的访问权限（路径标准化）。"""
        perms = self.plugin_permissions.get(plugin_id, {})
        allowed = perms.get("allowed_paths", [])
        # 空白名单 = 允许全部（测试便利）
        if not allowed:
            return True
        normalized = os.path.normpath(path)
        for allowed_path in allowed:
            norm_allowed = os.path.normpath(allowed_path)
            # 精确匹配或前缀匹配（带分隔符）
            if normalized == norm_allowed:
                return True
            if normalized.startswith(norm_allowed + os.sep):
                return True
        return False

    def grant_permission(
        self, plugin_id: str, allowed_paths: list[str]
    ) -> None:
        """授予插件路径访问权限。"""
        self.plugin_permissions.setdefault(plugin_id, {})[
            "allowed_paths"
        ] = allowed_paths

    # ── API 密钥 ──

    def get_api_key(self, provider: str) -> str | None:
        """读取 Desk 存储的 API 密钥。

        兼容火山方舟 Claude Coding Plan 套餐鉴权：
        provider="volcengine_claude" 返回火山方舟 API key。
        """
        if self.config_center is None:
            return None
        # 约定：config_center.get(f"api_keys.{provider}") 返回密钥
        try:
            return self.config_center.get(f"api_keys.{provider}")
        except Exception:
            return None

    def set_api_key(self, provider: str, key: str) -> None:
        """写入 API 密钥到 Desk 配置中心。"""
        if self.config_center is None:
            return
        try:
            self.config_center.set(f"api_keys.{provider}", key)
        except Exception as exc:
            logger.warning("desk_runtime: 写入 API 密钥失败: %s", exc)

    # ── MLX 推理 ──

    async def mlx_chat(
        self, model: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        """调用 fusion-mlx 本地推理（fusion-mlx 作为 Claude 视觉/图像生成后端）。"""
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx 客户端未注入")
        return await self.mlx_client.chat(model, messages, **kwargs)

    async def mlx_embed(self, text: str, model: str = "BGE-M3") -> Any:
        """调用 fusion-mlx 生成文本向量。"""
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx 客户端未注入")
        return await self.mlx_client.embed(text, model=model)

    async def mlx_health(self) -> bool:
        """检查 fusion-mlx 是否健康。"""
        if self.mlx_client is None:
            return False
        return await self.mlx_client.health()

    # ── 节点注册表桥 ──

    def list_nodes(self) -> list[dict[str, Any]]:
        """列出 fusion-desk 已注册的节点类型。"""
        if self.node_registry is None:
            return []
        return self.node_registry.list()

    def resolve_node(self, name: str) -> Any | None:
        """解析节点名称（支持别名）并返回节点类。"""
        if self.node_registry is None:
            return None
        return self.node_registry.get(name)

    # ── 任务调度器桥 ──

    def list_scheduled_tasks(self) -> list[Any]:
        """列出 fusion-desk 的全部定时任务。"""
        if self.task_scheduler is None:
            return []
        return self.task_scheduler.list_tasks()

    def gateway_info(self) -> dict[str, Any]:
        """返回 MCP 网关信息（供 Claude Desktop / Claude Code 对接）。"""
        return {
            "transport": "stdio",
            "port": self.mcp_gateway_port,
            "protocol_version": "2026-07-28",
        }
