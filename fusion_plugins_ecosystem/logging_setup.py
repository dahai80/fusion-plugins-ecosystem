"""结构化日志（P3-6）。

提供 JSON 日志 Formatter + correlation_id 贯穿单次请求：
- JsonFormatter：每条日志输出一行 JSON（ts/level/name/msg/correlation_id/...），
  便于聚合系统（ELK/Loki）检索与关联。
- correlation_id：contextvars.ContextVar，transport 接到请求时 stamp，
  handler 链路内所有日志自动带上同一 cid，跨模块可串联。

退化：JSON 格式化失败回退纯文本，绝不抛错吞日志。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# 当前请求关联 ID（transport 入口 set，handler 链路内 get）
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(cid: str | None) -> Any:
    """设置当前上下文 correlation_id，返回 token 供 reset_correlation_id 还原。"""
    return correlation_id.set(cid)


def reset_correlation_id(token: Any) -> None:
    """还原 correlation_id 到 set 前状态（请求结束调用）。"""
    correlation_id.reset(token)


def get_correlation_id() -> str | None:
    """读取当前上下文 correlation_id（日志 Formatter 调用）。"""
    return correlation_id.get()


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志 Formatter。

    输出字段：ts(ISO)/level/name/msg/correlation_id + record.extra(record 自带)。
    异常 traceback 入 exc_info 字段（多行字符串），不破坏 JSON 单行结构。
    """

    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            payload["correlation_id"] = cid
        # record 自带 extra 字段（logger.info(..., extra={...})）入 payload
        for key, val in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                try:
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = str(val)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            # JSON 序列化失败回退纯文本，绝不丢日志
            return (
                f'{{"ts":"{payload.get("ts", "")}","level":"{payload.get("level", "")}",'
                f'"name":"{payload.get("name", "")}","msg":'
                f"{json.dumps(record.getMessage(), ensure_ascii=False)}}}"
            )


def configure_structured_logging(level: int = logging.INFO) -> None:
    """配置根 logger 使用 JsonFormatter，输出到 stderr。

    幂等：重复调用只清旧 handler 再装，不叠加。
    """
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
