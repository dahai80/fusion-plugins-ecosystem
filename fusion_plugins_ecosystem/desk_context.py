"""Desk runtime 上下文桥。

封装 fusion-desk 提供的底层抽象，供插件生态统一调用：
- 模型上下文池（mlx_client）
- 本地文件权限（plugin_permissions）
- 硬件显存调度（vram_allocations + vram_total_mb）
- 外部工具代理端口（mcp_gateway_port）
- MCP 网关
- 全链路日志采集

设计原则：
- DeskContext 是 thin wrapper，真实能力由 DeskRuntime 提供
- 旧字段（runtime/hw_scheduler 等）保留为兼容入口
- 测试时注入 fake DeskRuntime 即可
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

logger = logging.getLogger(__name__)


@dataclass
class DeskContext:
    """fusion-desk runtime 上下文。

    由 Desk 在启动时注入，插件通过 PluginRegistry.desk 访问。

    Args:
        runtime: DeskRuntime 实例（封装 node_registry/task_scheduler/
                 mlx_client/config_center 等真实句柄）
        mcp_gateway_port: MCP 网关端口（由 Desk 分配，避免冲突）
        registered_plugin_ids: 已注册的插件 ID 集合（Desk 侧视图）
    """

    # Desk runtime 封装（真实或 fake）
    runtime: DeskRuntime | None = None
    # MCP 网关端口（兼容旧字段，从 runtime.mcp_gateway_port 同步）
    mcp_gateway_port: int | None = None
    # 已注册的插件 ID 集合
    registered_plugin_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """从 runtime 同步 mcp_gateway_port。"""
        if self.runtime is not None:
            if self.mcp_gateway_port is None:
                self.mcp_gateway_port = self.runtime.mcp_gateway_port

    def _ensure_runtime(self) -> DeskRuntime:
        """获取 runtime，未注入则返回空壳（降级为 no-op）。"""
        if self.runtime is None:
            self.runtime = DeskRuntime()
        return self.runtime

    def acquire_vram(self, plugin_id: str, mb: int) -> bool:
        """向 Desk 硬件调度器申请显存（解决「显存抢占冲突」痛点）。"""
        return self._ensure_runtime().acquire_vram(plugin_id, mb)

    def release_vram(self, plugin_id: str) -> None:
        """释放插件占用的显存。"""
        self._ensure_runtime().release_vram(plugin_id)

    def vram_usage(self) -> dict[str, int]:
        """返回当前显存台账快照。"""
        return self._ensure_runtime().vram_usage()

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
        self._ensure_runtime().log(plugin_id, level, message, **kwargs)

    def check_file_permission(self, plugin_id: str, path: str) -> bool:
        """检查插件是否具备对指定路径的访问权限。"""
        return self._ensure_runtime().check_file_permission(
            plugin_id, path
        )

    def grant_permission(
        self, plugin_id: str, allowed_paths: list[str]
    ) -> None:
        """授予插件路径访问权限。"""
        self._ensure_runtime().grant_permission(plugin_id, allowed_paths)

    def get_api_key(self, provider: str) -> str | None:
        """读取 Desk 存储的 API 密钥（兼容火山方舟 Claude Coding Plan 套餐鉴权）。"""
        return self._ensure_runtime().get_api_key(provider)

    def set_api_key(self, provider: str, key: str) -> None:
        """写入 API 密钥到 Desk 配置中心。"""
        self._ensure_runtime().set_api_key(provider, key)

    async def mlx_chat(
        self, model: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        """调用 fusion-mlx 本地推理（fusion-mlx 作为 Claude 视觉/图像生成后端）。"""
        return await self._ensure_runtime().mlx_chat(
            model, messages, **kwargs
        )

    async def mlx_embed(self, text: str, model: str = "BGE-M3") -> Any:
        """调用 fusion-mlx 生成文本向量。"""
        return await self._ensure_runtime().mlx_embed(text, model=model)

    async def mlx_health(self) -> bool:
        """检查 fusion-mlx 是否健康。"""
        return await self._ensure_runtime().mlx_health()

    def list_nodes(self) -> list[dict[str, Any]]:
        """列出 fusion-desk 已注册的节点类型。"""
        return self._ensure_runtime().list_nodes()

    def resolve_node(self, name: str) -> Any | None:
        """解析节点名称（支持别名）并返回节点类。"""
        return self._ensure_runtime().resolve_node(name)

    def list_scheduled_tasks(self) -> list[Any]:
        """列出 fusion-desk 的全部定时任务。"""
        return self._ensure_runtime().list_scheduled_tasks()

    def gateway_info(self) -> dict[str, Any]:
        """返回 MCP 网关信息（供 Claude Desktop / Claude Code 对接）。"""
        return self._ensure_runtime().gateway_info()
