"""结构化日志 + correlation_id 测试（P3-6a）。"""

from __future__ import annotations

import json
import logging

from fusion_plugins_ecosystem.logging_setup import (
    JsonFormatter,
    configure_structured_logging,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)


def _make_record(msg: str = "hello", **extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


# ── JsonFormatter ──


def test_json_formatter_outputs_valid_json() -> None:
    fmt = JsonFormatter()
    line = fmt.format(_make_record("ping"))
    payload = json.loads(line)
    assert payload["msg"] == "ping"
    assert payload["level"] == "INFO"
    assert payload["name"] == "test.logger"
    assert "ts" in payload


def test_json_formatter_includes_correlation_id() -> None:
    token = set_correlation_id("cid-abc-123")
    try:
        fmt = JsonFormatter()
        payload = json.loads(fmt.format(_make_record("with cid")))
        assert payload["correlation_id"] == "cid-abc-123"
    finally:
        reset_correlation_id(token)


def test_json_formatter_omits_cid_when_unset() -> None:
    assert get_correlation_id() is None
    fmt = JsonFormatter()
    payload = json.loads(fmt.format(_make_record("no cid")))
    assert "correlation_id" not in payload


def test_json_formatter_includes_extras() -> None:
    fmt = JsonFormatter()
    payload = json.loads(fmt.format(_make_record("ctx", plugin_id="p1", attempt=2)))
    assert payload["plugin_id"] == "p1"
    assert payload["attempt"] == 2


def test_json_formatter_includes_exc_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    rec = logging.LogRecord(
        name="test.logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=None,
        exc_info=exc_info,
    )
    fmt = JsonFormatter()
    payload = json.loads(fmt.format(rec))
    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]


def test_json_formatter_non_serializable_extra_falls_back_to_str() -> None:
    fmt = JsonFormatter()

    class _NotJson:
        pass

    payload = json.loads(fmt.format(_make_record("obj", thing=_NotJson())))
    assert "thing" in payload
    assert isinstance(payload["thing"], str)


# ── correlation_id contextvar ──


def test_set_reset_correlation_id() -> None:
    assert get_correlation_id() is None
    token = set_correlation_id("cid-1")
    assert get_correlation_id() == "cid-1"
    reset_correlation_id(token)
    assert get_correlation_id() is None


def test_set_correlation_id_replaces() -> None:
    t1 = set_correlation_id("a")
    t2 = set_correlation_id("b")
    assert get_correlation_id() == "b"
    reset_correlation_id(t2)
    assert get_correlation_id() == "a"
    reset_correlation_id(t1)
    assert get_correlation_id() is None


# ── configure_structured_logging ──


def test_configure_structured_logging_installs_json_handler() -> None:
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure_structured_logging(level=logging.DEBUG)
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def test_configure_structured_logging_idempotent() -> None:
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure_structured_logging()
        configure_structured_logging()
        configure_structured_logging()
        assert len(root.handlers) == 1
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
