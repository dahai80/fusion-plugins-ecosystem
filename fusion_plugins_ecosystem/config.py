"""生态配置面板（一键开关）。

对应 PRD「Claude 接入开关（生态配置面板）」：
ecosystem 后台提供一键开关：启用 Claude MCP 对外暴露、
自动导出插件为 Claude Skill、子代理超时自动销毁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EcosystemConfig:
    """生态配置（持久化到 Desk 配置中心）。

    所有开关默认 True，对应 PRD「原生完整兼容 Claude」的设计目标。
    """

    # ── Claude 全链路 ──
    # 启用 Claude MCP 对外暴露
    enable_claude_mcp: bool = True
    # 自动导出插件为 Claude Skill
    auto_export_claude_skill: bool = True
    # 子代理超时自动销毁（对应 PRD「子代理超时自动销毁」）
    subagent_timeout_destroy: bool = True
    # 默认挂载 token 压缩插件给 Claude 会话
    default_mount_compressor: bool = True

    # ── 火山方舟 Claude Coding Plan ──
    # 兼容火山方舟 Claude Coding Plan 套餐鉴权
    enable_volcengine_claude_plan: bool = True

    # ── 痛点配套 ──
    # 插件日志统一汇总到 Desk
    unified_log_to_desk: bool = True
    # 混合量化支持（fusion-mlx 本地推理作为 Claude 视觉/图像生成后端）
    enable_mixed_quantization: bool = True

    # ── 超时阈值 ──
    # 子代理默认超时秒数
    subagent_timeout_seconds: int = 600
    # 心跳超时阈值（秒）
    heartbeat_stale_seconds: int = 120
    # 最大自动重启次数
    max_auto_restart: int = 3

    def to_dict(self) -> dict[str, object]:
        """序列化为字典（供 Desk 配置中心持久化）。"""
        return {
            "enable_claude_mcp": self.enable_claude_mcp,
            "auto_export_claude_skill": self.auto_export_claude_skill,
            "subagent_timeout_destroy": self.subagent_timeout_destroy,
            "default_mount_compressor": self.default_mount_compressor,
            "enable_volcengine_claude_plan": self.enable_volcengine_claude_plan,
            "unified_log_to_desk": self.unified_log_to_desk,
            "enable_mixed_quantization": self.enable_mixed_quantization,
            "subagent_timeout_seconds": self.subagent_timeout_seconds,
            "heartbeat_stale_seconds": self.heartbeat_stale_seconds,
            "max_auto_restart": self.max_auto_restart,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "EcosystemConfig":
        """从字典反序列化（缺失字段使用默认值）。"""
        defaults = cls().to_dict()
        merged = {**defaults, **data}
        return cls(
            enable_claude_mcp=bool(merged["enable_claude_mcp"]),
            auto_export_claude_skill=bool(merged["auto_export_claude_skill"]),
            subagent_timeout_destroy=bool(merged["subagent_timeout_destroy"]),
            default_mount_compressor=bool(merged["default_mount_compressor"]),
            enable_volcengine_claude_plan=bool(
                merged["enable_volcengine_claude_plan"]
            ),
            unified_log_to_desk=bool(merged["unified_log_to_desk"]),
            enable_mixed_quantization=bool(merged["enable_mixed_quantization"]),
            subagent_timeout_seconds=int(merged["subagent_timeout_seconds"]),
            heartbeat_stale_seconds=int(merged["heartbeat_stale_seconds"]),
            max_auto_restart=int(merged["max_auto_restart"]),
        )
