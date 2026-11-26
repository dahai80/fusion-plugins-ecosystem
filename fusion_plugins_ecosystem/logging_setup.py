# Structured logging (P3-6).
# Provides a JSON log formatter + correlation_id threading through a single
# request:
# - JsonFormatter: emits each log as one JSON line
#   (ts/level/name/msg/correlation_id/...) so aggregation systems
#   (ELK/Loki) can search and correlate.
# - correlation_id: contextvars.ContextVar, stamped when the transport
#   receives a request; all logs in the handler chain automatically carry
#   the same cid and can be traced across modules.
# Degradation: JSON formatting failure falls back to plain text — never
# raises or swallows logs.

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# Current request correlation ID (set at the transport entry, read in the
# handler chain)
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(cid: str | None) -> Any:
    # Set the correlation_id for the current context; returns a token for
    # reset_correlation_id to restore.
    return correlation_id.set(cid)


def reset_correlation_id(token: Any) -> None:
    # Restore correlation_id to its pre-set state (call at request end).
    correlation_id.reset(token)


def get_correlation_id() -> str | None:
    # Read the correlation_id for the current context (used by the log formatter).
    return correlation_id.get()


class JsonFormatter(logging.Formatter):
    # Single-line JSON log formatter.
    # Output fields: ts(ISO)/level/name/msg/correlation_id +
    # record.extra (record-provided). Exception traceback goes into the
    # exc_info field (multi-line string) without breaking the single-line
    # JSON structure.

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
        # record-provided extra fields (logger.info(..., extra={...})) go
        # into the payload
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
            # JSON serialization failed; fall back to plain text, never lose the log
            return (
                f'{{"ts":"{payload.get("ts", "")}","level":"{payload.get("level", "")}",'
                f'"name":"{payload.get("name", "")}","msg":'
                f"{json.dumps(record.getMessage(), ensure_ascii=False)}}}"
            )


def configure_structured_logging(level: int = logging.INFO) -> None:
    # Configure the root logger to use JsonFormatter, output to stderr.
    # Idempotent: repeated calls remove old handlers before installing, no
    # stacking.
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
