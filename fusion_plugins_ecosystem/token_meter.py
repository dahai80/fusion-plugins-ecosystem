"""Token 统一计量分流。

解决 PRD 提到的痛点：
- 子代理跑 40 分钟无 token 消耗、卡死无日志
- 多插件 token 统计混乱

区分两种消耗：
- TokenKind.CLAUDE_MODEL：Claude 模型推理消耗（按 Claude API 计量）
- TokenKind.PLUGIN_LOCAL：插件本地计算开销（按 wall-clock + MLX token 计量）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TokenKind(str, Enum):
    """Token 消耗分类。"""

    CLAUDE_MODEL = "claude_model"        # Claude 模型推理
    PLUGIN_LOCAL = "plugin_local"        # 插件本地计算
    MLX_INFERENCE = "mlx_inference"      # fusion-mlx 本地推理
    MCP_RELAY = "mcp_relay"              # MCP 协议中继开销


@dataclass
class TokenRecord:
    """单次 token 消耗记录。"""

    plugin_id: str
    kind: TokenKind
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    wall_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)
    # 附加元数据（如 Claude model name、MLX model id）
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens


class TokenMeter:
    """Token 统一计量分流器。

    用法：
        meter = TokenMeter(desk_context)
        with meter.measure("caveman_compress", TokenKind.PLUGIN_LOCAL):
            ...  # 插件执行
    """

    def __init__(self, desk: Any | None = None) -> None:
        self.desk = desk
        self._records: list[TokenRecord] = []
        # 按插件 ID 聚合的统计
        self._by_plugin: dict[str, list[TokenRecord]] = {}

    def record(self, rec: TokenRecord) -> None:
        """记录一次 token 消耗。"""
        self._records.append(rec)
        self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
        # 异常检测：PLUGIN_LOCAL 但 input/output tokens 为 0 且 wall_seconds 很长
        # 对应「子代理跑 40 分钟无 token 消耗」痛点
        if (
            rec.kind == TokenKind.PLUGIN_LOCAL
            and rec.total_tokens == 0
            and rec.wall_seconds > 60
        ):
            logger.warning(
                "token_meter: 插件 %s 执行 %.1f 秒但无 token 消耗，疑似卡死",
                rec.plugin_id,
                rec.wall_seconds,
            )
            if self.desk is not None:
                self.desk.log(
                    rec.plugin_id,
                    "WARN",
                    "长时间无 token 消耗，疑似卡死",
                    wall_seconds=rec.wall_seconds,
                )

    def measure(
        self,
        plugin_id: str,
        kind: TokenKind = TokenKind.PLUGIN_LOCAL,
        input_tokens: int = 0,
        output_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ):
        """上下文管理器，自动测量 wall_seconds 并记录。

        用法：
            with meter.measure("caveman", TokenKind.PLUGIN_LOCAL):
                result = plugin.run(...)
        """
        return _MeasureContext(
            self,
            plugin_id,
            kind,
            input_tokens,
            output_tokens,
            metadata or {},
        )

    def summary(self) -> dict[str, dict[str, int]]:
        """按插件聚合统计：{plugin_id: {kind: total_tokens}}。"""
        summary: dict[str, dict[str, int]] = {}
        for rec in self._records:
            pid = rec.plugin_id
            summary.setdefault(pid, {})
            key = rec.kind.value
            summary[pid][key] = summary[pid].get(key, 0) + rec.total_tokens
        return summary

    def records_for(self, plugin_id: str) -> list[TokenRecord]:
        """返回指定插件的全部记录。"""
        return self._by_plugin.get(plugin_id, [])

    def all_records(self) -> list[TokenRecord]:
        """返回全部记录（按时间顺序）。"""
        return list(self._records)


class _MeasureContext:
    """measure() 的上下文管理器实现。"""

    def __init__(
        self,
        meter: TokenMeter,
        plugin_id: str,
        kind: TokenKind,
        input_tokens: int,
        output_tokens: int,
        metadata: dict[str, Any],
    ) -> None:
        self.meter = meter
        self.plugin_id = plugin_id
        self.kind = kind
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.metadata = metadata
        self._start: float | None = None

    def __enter__(self) -> "_MeasureContext":
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._start is None:
            return
        wall = time.time() - self._start
        rec = TokenRecord(
            plugin_id=self.plugin_id,
            kind=self.kind,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            wall_seconds=wall,
            metadata=self.metadata,
        )
        self.meter.record(rec)
