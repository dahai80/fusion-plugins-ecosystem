"""Token 统一计量分流。

解决 PRD 提到的痛点：
- 子代理跑 40 分钟无 token 消耗、卡死无日志
- 多插件 token 统计混乱

区分两种消耗：
- TokenKind.CLAUDE_MODEL：Claude 模型推理消耗（按 Claude API 计量）
- TokenKind.PLUGIN_LOCAL：插件本地计算开销（按 wall-clock + MLX token 计量）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TokenKind(str, Enum):
    """Token 消耗分类。"""

    CLAUDE_MODEL = "claude_model"  # Claude 模型推理
    PLUGIN_LOCAL = "plugin_local"  # 插件本地计算
    MLX_INFERENCE = "mlx_inference"  # fusion-mlx 本地推理
    MCP_RELAY = "mcp_relay"  # MCP 协议中继开销


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
        meter = TokenMeter(desk)
        with meter.measure("caveman_compress", TokenKind.PLUGIN_LOCAL):
            ...  # 插件执行
    """

    def __init__(
        self,
        desk: Any | None = None,
        max_records: int = 10000,
        persist_path: str | None = None,
    ) -> None:
        self.desk = desk
        self._max_records = max_records
        self._persist_path = persist_path
        self._records: list[TokenRecord] = []
        self._by_plugin: dict[str, list[TokenRecord]] = {}
        # 持久化锁：record 可被多协程并发调用，写文件必须串行（P1-7）
        self._save_lock = threading.Lock()
        if persist_path:
            self._load(persist_path)

    def record(self, rec: TokenRecord) -> None:
        """记录一次 token 消耗。"""
        self._records.append(rec)
        self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
        self._prune()
        self._save()
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

    def summary(self, since: float | None = None) -> dict[str, dict[str, int]]:
        """按插件聚合统计：{plugin_id: {kind: total_tokens}}。"""
        records = self._records_since(since) if since else self._records
        summary: dict[str, dict[str, int]] = {}
        for rec in records:
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

    def _prune(self) -> None:
        """淘汰超过 max_records 的旧记录。

        每个受影响 plugin 的 _by_plugin 列表一次性重建，不再对每条 removed 记录
        全量扫描做 is 过滤（P3-3，旧实现 O(N×M)）。
        """
        if len(self._records) <= self._max_records:
            return
        excess = len(self._records) - self._max_records
        removed = self._records[:excess]
        self._records = self._records[excess:]
        self._rebuild_by_plugin(removed)

    def _records_since(self, since: float) -> list[TokenRecord]:
        """返回指定时间之后的记录。

        逆序扫描 + 提前终止：记录按时间追加，逆序遍历遇到早于 since 即停（P3-2）。
        """
        out: list[TokenRecord] = []
        for rec in reversed(self._records):
            if rec.timestamp < since:
                break
            out.append(rec)
        out.reverse()
        return out

    def prune(self, max_age_seconds: float | None = None) -> None:
        """手动淘汰旧记录。

        - max_age_seconds 给定时：淘汰超过该秒数的旧记录（按时间淘汰）
        - 不论是否给定：再淘汰超过 max_records 的旧记录（按条数淘汰）
        """
        if max_age_seconds is not None:
            self._prune_by_age(max_age_seconds)
        self._prune()

    def _prune_by_age(self, max_age_seconds: float) -> None:
        """淘汰超过 max_age_seconds 秒的旧记录。"""
        cutoff = time.time() - max_age_seconds
        kept = [r for r in self._records if r.timestamp >= cutoff]
        if len(kept) == len(self._records):
            return
        removed = [r for r in self._records if r.timestamp < cutoff]
        self._records = kept
        self._rebuild_by_plugin(removed)
        logger.info(
            "token_meter: 按时间淘汰 %d 条记录（>%ss）", len(removed), max_age_seconds
        )

    def _rebuild_by_plugin(self, removed: list[TokenRecord]) -> None:
        """根据 removed 集合，一次性重建受影响 plugin 的 _by_plugin 列表。

        避免逐条 is 过滤全表（P3-3）。removed 多属同一 plugin 时收益最大。
        """
        if not removed:
            return
        removed_ids = {id(r) for r in removed}
        affected = {r.plugin_id for r in removed}
        for pid in affected:
            bucket = self._by_plugin.get(pid)
            if not bucket:
                continue
            self._by_plugin[pid] = [r for r in bucket if id(r) not in removed_ids]
            if not self._by_plugin[pid]:
                del self._by_plugin[pid]

    def _save(self) -> None:
        """持久化记录到 persist_path。

        写文件加锁 + 原子替换（temp + os.replace），避免并发 record 交错写损坏
        JSON（P1-7）。全量重写仍在，但串行化保证一致性；高频场景应由调用方
        批量记录后单次 flush，或上层降频（本计量器不自行节流以免丢记录）。
        """
        if not self._persist_path:
            return
        try:
            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "plugin_id": r.plugin_id,
                    "kind": r.kind.value,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "total_tokens": r.total_tokens,
                    "wall_seconds": r.wall_seconds,
                    "timestamp": r.timestamp,
                    "metadata": r.metadata,
                }
                for r in self._records
            ]
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            with self._save_lock:
                # 写临时文件再原子 rename，中途崩溃不会留半截损坏文件
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(path.parent), prefix=".token_meter_", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(payload)
                    os.replace(tmp_path, str(path))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
        except Exception as e:
            logger.warning("token_meter: persist failed: %s", e)

    def _load(self, path_str: str) -> None:
        """从 persist_path 加载记录。"""
        try:
            path = Path(path_str)
            if not path.exists():
                return
            data = json.loads(path.read_text())
            if not isinstance(data, list):
                return
            for item in data:
                kind_str = item.get("kind", "plugin_local")
                try:
                    kind = TokenKind(kind_str)
                except ValueError:
                    kind = TokenKind.PLUGIN_LOCAL
                rec = TokenRecord(
                    plugin_id=item.get("plugin_id", "unknown"),
                    kind=kind,
                    input_tokens=item.get("input_tokens", 0),
                    output_tokens=item.get("output_tokens", 0),
                    total_tokens=item.get("total_tokens", 0),
                    wall_seconds=item.get("wall_seconds", 0.0),
                    timestamp=item.get("timestamp", time.time()),
                    metadata=item.get("metadata", {}),
                )
                self._records.append(rec)
                self._by_plugin.setdefault(rec.plugin_id, []).append(rec)
            self._prune()
            logger.info("token_meter: loaded %d records from %s", len(data), path_str)
        except Exception as e:
            logger.warning("token_meter: load failed: %s", e)


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


class AsyncMeasureContext:
    """measure() 的异步上下文管理器实现。"""

    def __init__(
        self,
        meter: TokenMeter,
        plugin_id: str,
        kind: TokenKind,
        input_tokens: int = 0,
        output_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.meter = meter
        self.plugin_id = plugin_id
        self.kind = kind
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.metadata = metadata or {}
        self._start: float | None = None

    async def __aenter__(self) -> "AsyncMeasureContext":
        self._start = time.time()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
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
