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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode

logger = logging.getLogger(__name__)


class PluginCategory(str, Enum):
    """插件分类（对应 PRD「Claude Code 专属插件分类」）。"""

    CODING_PLAN = "coding_plan"          # coding-plan 加速插件
    CONTEXT_COMPRESS = "context_compress"  # 代码上下文压缩（caveman 等）
    MLX_INFERENCE = "mlx_inference"       # 本地 MLX 模型推理插件
    TERMINAL_PROXY = "terminal_proxy"     # 终端命令代理插件
    FILE_INDEX = "file_index"             # 本地文件检索
    QUANTIZATION = "quantization"         # 混合量化工具
    VISUAL_BACKEND = "visual_backend"     # Claude 视觉/图像生成后端
    CUSTOM = "custom"


class PluginCapability(str, Enum):
    """插件能力声明，驱动 MCP Tools / Claude Skill 自动暴露。"""

    MCP_TOOL = "mcp_tool"                # 暴露为 MCP Tool
    CLAUDE_SKILL = "claude_skill"        # 自动转 Claude Skill
    SUBAGENT = "subagent"                # Claude Code 子代理
    FILE_ACCESS = "file_access"          # 本地文件读写
    VRAM_CONSUMER = "vram_consumer"      # 占用显存，需 Desk 调度
    LONG_TASK = "long_task"              # 长任务，需超时熔断


@dataclass(frozen=True)
class PluginParam:
    """插件参数 schema（用于 Claude Skill 参数描述 + Desk 配置面板）。"""

    name: str
    type: PluginParamType | str    # PluginParamType 枚举，向后兼容字符串
    description: str
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PluginManifest:
    """插件清单（声明式，不含实例，不可变）。"""

    id: str                                    # 全局唯一，如 "caveman_compress"
    name: str                                  # 用户友好名称
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


class PluginRegistry:
    """插件注册中心。

    用法：
        registry = PluginRegistry(desk_context)
        registry.register(manifest)
        registry.register_builtin()    # 注册内置 caveman 等
    """

    def __init__(self, desk: DeskRuntime | None = None) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self.desk: DeskRuntime = desk or DeskRuntime()

    def register(self, manifest: PluginManifest) -> None:
        """注册插件清单。"""
        if manifest.id in self._manifests:
            raise ValueError(f"插件 {manifest.id!r} 已注册")
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

    def register_builtin(self) -> None:
        """注册内置插件（自动扫描 builtin 包）。

        扫描 fusion_plugins_ecosystem.builtin 包下所有含 *_MANIFEST
        模块级变量的模块，自动注册。
        """
        import importlib
        import pkgutil

        import fusion_plugins_ecosystem.builtin as builtin_pkg

        for importer, mod_name, is_pkg in pkgutil.iter_modules(
            builtin_pkg.__path__
        ):
            if is_pkg:
                continue
            fqn = f"fusion_plugins_ecosystem.builtin.{mod_name}"
            try:
                mod = importlib.import_module(fqn)
            except Exception as exc:
                logger.warning("registry: 跳过内置模块 %s: %s", fqn, exc)
                continue
            for attr_name in dir(mod):
                if attr_name.endswith("_MANIFEST"):
                    manifest = getattr(mod, attr_name)
                    if isinstance(manifest, PluginManifest):
                        self.register(manifest)

    def default_mounted(self) -> list[PluginManifest]:
        """返回所有 default_mounted=True 的插件（默认挂载给 Claude 会话）。"""
        return [m for m in self._manifests.values() if m.default_mounted]
