"""生态配置面板（一键开关）。

对应 PRD「Claude 接入开关（生态配置面板）」：
ecosystem 后台提供一键开关：启用 Claude MCP 对外暴露、
自动导出插件为 Claude Skill、子代理超时自动销毁。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

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

    # ── MCP 服务 ──
    # MCP 传输类型
    mcp_transport: str = "stdio"
    # MCP 监听主机
    mcp_host: str = "127.0.0.1"
    # MCP 监听端口（0 = auto）
    mcp_port: int = 0

    # ── 沙箱 ──
    # 默认沙箱模式
    sandbox_default_mode: str = "inline"

    # ── 计量 ──
    # Token 记录最大条数
    max_token_records: int = 10000
    # Token 记录持久化路径（None=纯内存）
    token_persist_path: str | None = None

    # ── 变更回调 ──
    _observers: list[Callable[[str, Any, Any], None]] = field(
        default_factory=list, repr=False
    )

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
            "mcp_transport": self.mcp_transport,
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "sandbox_default_mode": self.sandbox_default_mode,
            "max_token_records": self.max_token_records,
            "token_persist_path": self.token_persist_path,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, object]
    ) -> tuple["EcosystemConfig", list[str]]:
        """从字典反序列化，返回 (config, warnings)。"""
        defaults = cls().to_dict()
        merged = {**defaults, **data}
        warnings: list[str] = []

        def _safe_bool(key: str) -> bool:
            val = merged[key]
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            warnings.append(f"config.{key}: 类型不匹配 {type(val).__name__}, 使用默认值")
            return defaults[key]  # type: ignore[return-value]

        def _safe_int(key: str, min_val: int = 0, max_val: int = 100000) -> int:
            val = merged[key]
            try:
                result = int(val)  # type: ignore[arg-type]
                if result < min_val or result > max_val:
                    warnings.append(f"config.{key}: {result} 超出范围 [{min_val},{max_val}]")
                    return defaults[key]  # type: ignore[return-value]
                return result
            except (ValueError, TypeError):
                warnings.append(f"config.{key}: 类型不匹配 {type(val).__name__}, 使用默认值")
                return defaults[key]  # type: ignore[return-value]

        def _safe_str(key: str, allowed: tuple[str, ...] | None = None) -> str:
            val = merged[key]
            if isinstance(val, str):
                if allowed and val not in allowed:
                    warnings.append(f"config.{key}: {val!r} 不在 {allowed} 中, 使用默认值")
                    return defaults[key]  # type: ignore[return-value]
                return val
            warnings.append(f"config.{key}: 类型不匹配 {type(val).__name__}, 使用默认值")
            return defaults[key]  # type: ignore[return-value]

        config = cls(
            enable_claude_mcp=_safe_bool("enable_claude_mcp"),
            auto_export_claude_skill=_safe_bool("auto_export_claude_skill"),
            subagent_timeout_destroy=_safe_bool("subagent_timeout_destroy"),
            default_mount_compressor=_safe_bool("default_mount_compressor"),
            enable_volcengine_claude_plan=_safe_bool("enable_volcengine_claude_plan"),
            unified_log_to_desk=_safe_bool("unified_log_to_desk"),
            enable_mixed_quantization=_safe_bool("enable_mixed_quantization"),
            subagent_timeout_seconds=_safe_int("subagent_timeout_seconds", 1),
            heartbeat_stale_seconds=_safe_int("heartbeat_stale_seconds", 1),
            max_auto_restart=_safe_int("max_auto_restart", 0, 100),
            mcp_transport=_safe_str("mcp_transport", ("stdio", "sse", "http")),
            mcp_host=_safe_str("mcp_host"),
            mcp_port=_safe_int("mcp_port", 0, 65535),
            sandbox_default_mode=_safe_str("sandbox_default_mode", ("inline", "process")),
            max_token_records=_safe_int("max_token_records", 100),
            token_persist_path=merged.get("token_persist_path"),
        )

        for w in warnings:
            logger.warning("config: %s", w)

        return config, warnings

    def validate(self) -> list[str]:
        """返回验证错误列表（空列表表示全部合法）。"""
        errors: list[str] = []
        if self.subagent_timeout_seconds <= 0:
            errors.append("subagent_timeout_seconds must be > 0")
        if self.heartbeat_stale_seconds <= 0:
            errors.append("heartbeat_stale_seconds must be > 0")
        if self.max_auto_restart < 0:
            errors.append("max_auto_restart must be >= 0")
        if self.mcp_transport not in ("stdio", "sse", "http"):
            errors.append(f"mcp_transport must be stdio/sse/http, got {self.mcp_transport!r}")
        if self.mcp_port < 0 or self.mcp_port > 65535:
            errors.append(f"mcp_port must be 0-65535, got {self.mcp_port}")
        return errors

    def add_observer(self, callback: Callable[[str, Any, Any], None]) -> None:
        """添加配置变更回调。"""
        self._observers.append(callback)

    def _notify_change(self, key: str, old_value: Any, new_value: Any) -> None:
        """通知配置变更。"""
        for obs in self._observers:
            try:
                obs(key, old_value, new_value)
            except Exception as exc:
                logger.warning("config: observer 异常: %s", exc)
