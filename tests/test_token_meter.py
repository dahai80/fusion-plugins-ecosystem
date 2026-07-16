"""TokenMeter 测试。"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from fusion_plugins_ecosystem.token_meter import (
    TokenKind,
    TokenMeter,
    TokenRecord,
)


# ── TokenRecord ──

def test_token_record_default_total_is_sum() -> None:
    rec = TokenRecord(
        plugin_id="p1",
        kind=TokenKind.CLAUDE_MODEL,
        input_tokens=100,
        output_tokens=50,
    )
    assert rec.total_tokens == 150


def test_token_record_explicit_total_preserved() -> None:
    rec = TokenRecord(
        plugin_id="p1",
        kind=TokenKind.CLAUDE_MODEL,
        input_tokens=100,
        output_tokens=50,
        total_tokens=999,
    )
    assert rec.total_tokens == 999


def test_token_record_defaults_zero() -> None:
    rec = TokenRecord(plugin_id="p1", kind=TokenKind.PLUGIN_LOCAL)
    assert rec.input_tokens == 0
    assert rec.output_tokens == 0
    assert rec.total_tokens == 0
    assert rec.wall_seconds == 0.0
    assert rec.metadata == {}


# ── TokenMeter.record ──

def test_record_appends_to_records() -> None:
    meter = TokenMeter()
    meter.record(
        TokenRecord("p1", TokenKind.CLAUDE_MODEL, 10, 20)
    )
    assert len(meter.all_records()) == 1
    assert meter.all_records()[0].total_tokens == 30


def test_record_long_no_token_warns() -> None:
    meter = TokenMeter()
    rec = TokenRecord(
        "p1",
        TokenKind.PLUGIN_LOCAL,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=120,
    )
    meter.record(rec)  # 应触发 warning，不抛异常
    assert len(meter.all_records()) == 1


def test_record_short_no_token_no_warn() -> None:
    meter = TokenMeter()
    rec = TokenRecord(
        "p1",
        TokenKind.PLUGIN_LOCAL,
        wall_seconds=10,
    )
    meter.record(rec)
    assert len(meter.all_records()) == 1


def test_record_with_desk_logs_warning() -> None:
    desk_mock = MagicMock()
    meter = TokenMeter(desk=desk_mock)
    rec = TokenRecord(
        "p1",
        TokenKind.PLUGIN_LOCAL,
        wall_seconds=120,
    )
    meter.record(rec)
    # desk.log 应被调用
    assert desk_mock.log.called


def test_record_with_tokens_does_not_warn() -> None:
    desk_mock = MagicMock()
    meter = TokenMeter(desk=desk_mock)
    rec = TokenRecord(
        "p1",
        TokenKind.PLUGIN_LOCAL,
        input_tokens=50,
        output_tokens=50,
        wall_seconds=120,
    )
    meter.record(rec)
    # 有 token 消耗，不应触发 warning log
    assert not desk_mock.log.called


# ── measure context manager ──

def test_measure_records_wall_seconds() -> None:
    meter = TokenMeter()
    with meter.measure("p1", TokenKind.PLUGIN_LOCAL):
        time.sleep(0.05)
    records = meter.records_for("p1")
    assert len(records) == 1
    assert records[0].wall_seconds > 0.04


def test_measure_with_input_output_tokens() -> None:
    meter = TokenMeter()
    with meter.measure(
        "p1",
        TokenKind.CLAUDE_MODEL,
        input_tokens=100,
        output_tokens=200,
    ):
        pass
    rec = meter.records_for("p1")[0]
    assert rec.input_tokens == 100
    assert rec.output_tokens == 200
    assert rec.total_tokens == 300


def test_measure_with_metadata() -> None:
    meter = TokenMeter()
    with meter.measure(
        "p1",
        TokenKind.CLAUDE_MODEL,
        metadata={"model": "claude-opus-4"},
    ):
        pass
    rec = meter.records_for("p1")[0]
    assert rec.metadata == {"model": "claude-opus-4"}


def test_measure_exception_still_records() -> None:
    meter = TokenMeter()
    try:
        with meter.measure("p1", TokenKind.PLUGIN_LOCAL):
            raise ValueError("boom")
    except ValueError:
        pass
    # 即使抛异常，记录也应写入
    assert len(meter.records_for("p1")) == 1


# ── summary / records_for / all_records ──

def test_summary_aggregates_by_plugin_and_kind() -> None:
    meter = TokenMeter()
    meter.record(
        TokenRecord("p1", TokenKind.CLAUDE_MODEL, 100, 50)
    )
    meter.record(
        TokenRecord("p1", TokenKind.PLUGIN_LOCAL, 30, 20)
    )
    meter.record(
        TokenRecord("p2", TokenKind.CLAUDE_MODEL, 200, 100)
    )
    summary = meter.summary()
    assert summary["p1"]["claude_model"] == 150
    assert summary["p1"]["plugin_local"] == 50
    assert summary["p2"]["claude_model"] == 300


def test_summary_empty() -> None:
    meter = TokenMeter()
    assert meter.summary() == {}


def test_records_for_unknown_returns_empty() -> None:
    meter = TokenMeter()
    assert meter.records_for("unknown") == []


def test_records_for_returns_plugin_records() -> None:
    meter = TokenMeter()
    meter.record(TokenRecord("p1", TokenKind.CLAUDE_MODEL, 10, 5))
    meter.record(TokenRecord("p1", TokenKind.PLUGIN_LOCAL, 3, 2))
    meter.record(TokenRecord("p2", TokenKind.CLAUDE_MODEL, 100, 50))
    p1_records = meter.records_for("p1")
    assert len(p1_records) == 2


def test_all_returns_all_in_order() -> None:
    meter = TokenMeter()
    for i in range(5):
        meter.record(
            TokenRecord(f"p{i}", TokenKind.CLAUDE_MODEL, i, i)
        )
    all_records = meter.all_records()
    assert len(all_records) == 5
    # 按插入顺序
    assert all_records[0].plugin_id == "p0"
    assert all_records[4].plugin_id == "p4"


# ── TokenKind enum ──

def test_token_kind_values() -> None:
    assert TokenKind.CLAUDE_MODEL.value == "claude_model"
    assert TokenKind.PLUGIN_LOCAL.value == "plugin_local"
    assert TokenKind.MLX_INFERENCE.value == "mlx_inference"
    assert TokenKind.MCP_RELAY.value == "mcp_relay"
