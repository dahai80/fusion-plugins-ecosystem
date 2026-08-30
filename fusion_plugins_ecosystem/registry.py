"""插件注册中心。

标准化插件元数据、参数 schema、能力声明。所有生态插件（caveman 压缩、
量化工具、代码分析、本地文件检索）都通过本中心注册。

设计原则：
- 注册中心不持有插件实例，只持有 manifest（声明式）
- 实例化由 PluginLifecycle 按需进行，便于热重载
- manifest 同时驱动 Claude Skill 自动生成
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode

logger = logging.getLogger(__name__)


class PluginCategory(str, Enum):
    """插件分类（对应 PRD「Claude Code 专属插件分类」）。"""

    CODING_PLAN = "coding_plan"  # coding-plan 加速插件
    CONTEXT_COMPRESS = "context_compress"  # 代码上下文压缩（caveman 等）
    MLX_INFERENCE = "mlx_inference"  # 本地 MLX 模型推理插件
    TERMINAL_PROXY = "terminal_proxy"  # 终端命令代理插件
    FILE_INDEX = "file_index"  # 本地文件检索
    QUANTIZATION = "quantization"  # 混合量化工具
    VISUAL_BACKEND = "visual_backend"  # Claude 视觉/图像生成后端
    CUSTOM = "custom"


class PluginCapability(str, Enum):
    """插件能力声明，驱动 MCP Tools / Claude Skill 自动暴露。"""

    MCP_TOOL = "mcp_tool"  # 暴露为 MCP Tool
    CLAUDE_SKILL = "claude_skill"  # 自动转 Claude Skill
    SUBAGENT = "subagent"  # Claude Code 子代理
    FILE_ACCESS = "file_access"  # 本地文件读写
    VRAM_CONSUMER = "vram_consumer"  # 占用显存，需 Desk 调度
    LONG_TASK = "long_task"  # 长任务，需超时熔断


@dataclass(frozen=True)
class PluginParam:
    """插件参数 schema（用于 Claude Skill 参数描述 + Desk 配置面板）。"""

    name: str
    type: PluginParamType | str  # PluginParamType 枚举，向后兼容字符串
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
    """插件清单（声明式，不含实例，不可变）。"""

    id: str  # 全局唯一，如 "caveman_compress"
    name: str  # 用户友好名称
    version: str
    category: PluginCategory
    description: str
    capabilities: tuple[PluginCapability, ...] = ()
    params: tuple[PluginParam, ...] = ()
    # 插件入口（可调用对象或 dotted path）
    entry_point: Callable[..., Any] | str | None = None
    # 默认是否挂载给 Claude 会话（对应 PRD「内置 caveman 默认挂载」）
    default_mounted: bool = False
    # 长任务超时秒数（None 表示继承 Desk 默认）
    timeout_seconds: int | None = None
    # 显存占用预估（MB），0 表示不占用
    vram_mb: int = 0
    # 依赖的其他插件 ID（拓扑序加载）
    depends_on: tuple[str, ...] = ()
    # 沙箱运行模式
    sandbox_mode: SandboxMode = SandboxMode.INLINE
    # 插件级最大重启次数（None 表示继承全局 MAX_RESTART）
    max_restart: int | None = None
    # MCP 工具输出 JSON Schema（MCP 2026-07-28 outputSchema）
    output_schema: dict[str, Any] | None = None
    # MCP 工具行为注解
    mcp_annotations: Any = None
    # 子代理使用的 Claude 模型（None 表示使用默认模型）
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
            "mcp_annotations": self.mcp_annotations.to_dict()
            if self.mcp_annotations
            else None,
            "agent_model": self.agent_model,
        }


class PluginRegistry:
    """插件注册中心。

    用法：
        registry = PluginRegistry(desk)
        registry.register(manifest)
        registry.register_builtin()    # 注册内置 caveman 等
    """

    def __init__(self, desk: DeskRuntime | None = None) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self.desk: DeskRuntime = desk or DeskRuntime()

    def register(self, manifest: PluginManifest) -> None:
        """注册插件清单。相同 ID + 相同版本幂等；不同版本拒绝。"""
        existing = self._manifests.get(manifest.id)
        if existing is not None:
            if existing.version == manifest.version:
                logger.debug(
                    "registry: 插件 %s v%s 重复注册（幂等忽略）",
                    manifest.id,
                    manifest.version,
                )
                return
            raise ValueError(
                f"插件 {manifest.id!r} 版本冲突: 已注册 v{existing.version}, 尝试注册 v{manifest.version}"
            )
        self._manifests[manifest.id] = manifest
        self.desk.registered_plugin_ids.add(manifest.id)
        logger.info("registry: 插件 %s v%s 已注册", manifest.id, manifest.version)

    def unregister(self, plugin_id: str) -> None:
        """注销插件。"""
        if plugin_id in self._manifests:
            del self._manifests[plugin_id]
            self.desk.registered_plugin_ids.discard(plugin_id)
            logger.info("registry: 插件 %s 已注销", plugin_id)

    def get(self, plugin_id: str) -> PluginManifest | None:
        """获取插件清单。"""
        return self._manifests.get(plugin_id)

    def list(self, category: PluginCategory | None = None) -> list[PluginManifest]:
        """列出插件（可按分类过滤）。"""
        result = list(self._manifests.values())
        if category is not None:
            result = [m for m in result if m.category == category]
        return result

    def list_as_dicts(
        self, category: PluginCategory | None = None
    ) -> list[dict[str, Any]]:
        """列出插件为 JSON 友好字典（供 Swift/Kotlin/TS 消费端）。"""
        return [m.to_dict() for m in self.list(category=category)]

    def resolve_load_order(self, plugin_ids: list[str] | None = None) -> list[str]:
        """按依赖拓扑排序返回加载顺序。

        Args:
            plugin_ids: 需要加载的插件 ID 列表，None 表示全部。

        Returns:
            排序后的插件 ID 列表（依赖在前）。

        Raises:
            ValueError: 存在循环依赖。
            KeyError: 依赖的插件未注册。
        """
        targets = set(plugin_ids) if plugin_ids else set(self._manifests.keys())
        visited: set[str] = set()
        order: list[str] = []
        in_stack: set[str] = set()

        def visit(pid: str) -> None:
            if pid in visited:
                return
            if pid in in_stack:
                raise ValueError(f"循环依赖: {' → '.join(in_stack)} → {pid}")
            manifest = self._manifests.get(pid)
            if manifest is None:
                raise KeyError(f"依赖插件 {pid!r} 未注册")
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
        """注册内置插件（显式聚合清单）。

        新增内置插件时在此追加。不再用 pkgutil+dir() 扫描：dir() 会拾起
        被导入到模块命名空间的 *_MANIFEST 符号（误注册非内置插件）、顺序
        不稳定、且无法在导入期静态发现新增模块（P2-6）。
        """
        from fusion_plugins_ecosystem.builtin.caveman_compress import (
            CAVEMAN_MANIFEST,
        )

        builtin_manifests: list[PluginManifest] = [
            CAVEMAN_MANIFEST,
        ]
        for manifest in builtin_manifests:
            self.register(manifest)

    def default_mounted(self) -> list[PluginManifest]:
        """返回所有 default_mounted=True 的插件（默认挂载给 Claude 会话）。"""
        return [m for m in self._manifests.values() if m.default_mounted]
