"""TokenMeter tests."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

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
    meter.record(TokenRecord("p1", TokenKind.CLAUDE_MODEL, 10, 20))
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
    meter.record(rec)  # should trigger a warning, not raise
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
    # desk.log should be called
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
    # Token consumption present, so no warning log should fire
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
    # Even on exception the record should be written
    assert len(meter.records_for("p1")) == 1


# ── summary / records_for / all_records ──


def test_summary_aggregates_by_plugin_and_kind() -> None:
    meter = TokenMeter()
    meter.record(TokenRecord("p1", TokenKind.CLAUDE_MODEL, 100, 50))
    meter.record(TokenRecord("p1", TokenKind.PLUGIN_LOCAL, 30, 20))
    meter.record(TokenRecord("p2", TokenKind.CLAUDE_MODEL, 200, 100))
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
        meter.record(TokenRecord(f"p{i}", TokenKind.CLAUDE_MODEL, i, i))
    all_records = meter.all_records()
    assert len(all_records) == 5
    # In insertion order
    assert all_records[0].plugin_id == "p0"
    assert all_records[4].plugin_id == "p4"


# ── TokenKind enum ──


def test_token_kind_values() -> None:
    assert TokenKind.CLAUDE_MODEL.value == "claude_model"
    assert TokenKind.PLUGIN_LOCAL.value == "plugin_local"
    assert TokenKind.MLX_INFERENCE.value == "mlx_inference"
    assert TokenKind.MCP_RELAY.value == "mcp_relay"


# ── P3-4: append-write + periodic compaction ──


def test_wal_append_roundtrip(tmp_path) -> None:
    """record -> flush -> a fresh meter loads the same records (WAL compacted
    into a snapshot; single file recoverable)."""
    import json
    import os

    path = str(tmp_path / "tokens.json")
    m1 = TokenMeter(persist_path=path, save_batch=1)
    m1.record(TokenRecord("p1", TokenKind.CLAUDE_MODEL, 10, 20))
    m1.record(TokenRecord("p2", TokenKind.PLUGIN_LOCAL, 5, 5))
    m1.flush()
    assert os.path.exists(path)
    m2 = TokenMeter(persist_path=path)
    recs = m2.all_records()
    assert len(recs) == 2
    assert recs[0].plugin_id == "p1"
    assert recs[0].total_tokens == 30
    assert recs[1].plugin_id == "p2"
    assert json.loads(open(path).read())["last_seq"] >= 2


def test_wal_replay_dedups_after_snapshot(tmp_path) -> None:
    """Crash-recovery dedup: wal rows already covered by snapshot.last_seq are not loaded twice.

    Simulates: write snapshot(last_seq=1) + wal containing rows with seq<=1 ->
    load keeps only the non-duplicate records.
    """
    import json

    path = str(tmp_path / "tokens.json")
    wal_path = path + ".wal"
    with open(path, "w") as f:
        json.dump(
            {
                "last_seq": 1,
                "records": [
                    {
                        "plugin_id": "snap_p",
                        "kind": "claude_model",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "wall_seconds": 0.0,
                        "timestamp": 1000.0,
                        "metadata": {},
                    }
                ],
            },
            f,
        )
    # wal contains seq=1 (already in snapshot) and seq=2 (new)
    with open(wal_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "plugin_id": "dup_p",
                    "kind": "plugin_local",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "wall_seconds": 0.0,
                    "timestamp": 2000.0,
                    "metadata": {},
                    "seq": 1,
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "plugin_id": "new_p",
                    "kind": "plugin_local",
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                    "wall_seconds": 0.0,
                    "timestamp": 3000.0,
                    "metadata": {},
                    "seq": 2,
                }
            )
            + "\n"
        )
    m = TokenMeter(persist_path=path)
    recs = m.all_records()
    assert len(recs) == 2
    pids = [r.plugin_id for r in recs]
    assert "dup_p" not in pids
    assert "new_p" in pids
    assert m._next_seq == 3


def test_wal_compaction_triggers_on_threshold(tmp_path) -> None:
    """wal lines beyond _compact_threshold -> snapshot written and wal cleared."""
    import os

    path = str(tmp_path / "tokens.json")
    # Very low threshold forces compaction
    m = TokenMeter(persist_path=path, save_batch=1, max_records=100)
    m._compact_threshold = 3
    for i in range(4):
        m.record(TokenRecord(f"p{i}", TokenKind.PLUGIN_LOCAL, i, i))
    # After the 3rd record, wal_line_count>=3 triggers compaction -> snapshot on disk
    assert os.path.exists(path)
    # flush forces compaction; after shutdown a single file restores all 4 records
    m.flush()
    assert m._wal_line_count == 0
    assert m._next_seq == 5
    snapshot_records = json_load_records(path)
    assert len(snapshot_records) == 4


def test_wal_appends_without_snapshot_until_compact(tmp_path) -> None:
    """In the steady state _save only appends to the wal, no snapshot (until threshold/flush)."""
    import os

    path = str(tmp_path / "tokens.json")
    wal_path = path + ".wal"
    m = TokenMeter(persist_path=path, save_batch=1, max_records=1000)
    m._compact_threshold = 1000
    for i in range(3):
        m.record(TokenRecord(f"p{i}", TokenKind.PLUGIN_LOCAL, i, i))
    # 3 records < threshold -> no snapshot, wal exists with 3 lines
    assert not os.path.exists(path)
    assert os.path.exists(wal_path)
    lines = [ln for ln in open(wal_path).read().splitlines() if ln.strip()]
    assert len(lines) == 3


def json_load_records(path: str) -> list:
    import json

    raw = json.loads(open(path).read())
    return raw["records"] if isinstance(raw, dict) else raw
