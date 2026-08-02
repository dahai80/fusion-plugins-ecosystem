"""TokenMeter persist/prune + EcosystemConfig token_persist_path 测试。"""

from __future__ import annotations

import json
import os
import tempfile


from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter, TokenRecord


# ── TokenMeter prune ──


def test_prune_public_method() -> None:
    meter = TokenMeter(max_records=5)
    for i in range(10):
        meter.record(TokenRecord(plugin_id=f"p{i}", kind=TokenKind.PLUGIN_LOCAL))
    assert len(meter.all_records()) == 5
    meter.prune()
    assert len(meter.all_records()) == 5


def test_prune_noop_under_limit() -> None:
    meter = TokenMeter(max_records=100)
    meter.record(TokenRecord(plugin_id="p1", kind=TokenKind.PLUGIN_LOCAL))
    meter.prune()
    assert len(meter.all_records()) == 1


# ── TokenMeter persist ──


def test_persist_saves_to_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tokens.json")
        meter = TokenMeter(persist_path=path)
        meter.record(
            TokenRecord(
                plugin_id="test_p",
                kind=TokenKind.CLAUDE_MODEL,
                input_tokens=10,
                output_tokens=5,
            )
        )
        assert os.path.exists(path)
        data = json.loads(open(path).read())
        assert len(data) == 1
        assert data[0]["plugin_id"] == "test_p"
        assert data[0]["kind"] == "claude_model"


def test_persist_load_from_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tokens.json")
        data = [
            {
                "plugin_id": "loaded_p",
                "kind": "plugin_local",
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "wall_seconds": 1.5,
                "timestamp": 1000000.0,
                "metadata": {},
            }
        ]
        with open(path, "w") as f:
            json.dump(data, f)

        meter = TokenMeter(persist_path=path)
        records = meter.all_records()
        assert len(records) == 1
        assert records[0].plugin_id == "loaded_p"
        assert records[0].input_tokens == 20


def test_persist_load_missing_file() -> None:
    meter = TokenMeter(persist_path="/nonexistent/path/tokens.json")
    assert len(meter.all_records()) == 0


def test_persist_none_no_file() -> None:
    meter = TokenMeter(persist_path=None)
    meter.record(TokenRecord(plugin_id="p1", kind=TokenKind.PLUGIN_LOCAL))
    assert len(meter.all_records()) == 1


def test_persist_load_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.json")
        with open(path, "w") as f:
            f.write("not json")
        meter = TokenMeter(persist_path=path)
        assert len(meter.all_records()) == 0


def test_persist_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "roundtrip.json")

        meter1 = TokenMeter(persist_path=path)
        meter1.record(
            TokenRecord(
                plugin_id="rt_p",
                kind=TokenKind.MLX_INFERENCE,
                input_tokens=100,
                output_tokens=50,
                wall_seconds=2.0,
                metadata={"model": "llama"},
            )
        )

        meter2 = TokenMeter(persist_path=path)
        records = meter2.all_records()
        assert len(records) == 1
        assert records[0].plugin_id == "rt_p"
        assert records[0].kind == TokenKind.MLX_INFERENCE
        assert records[0].metadata["model"] == "llama"


# ── EcosystemConfig token_persist_path ──


def test_config_token_persist_path_default() -> None:
    config = EcosystemConfig()
    assert config.token_persist_path is None


def test_config_token_persist_path_to_dict() -> None:
    config = EcosystemConfig(token_persist_path="/tmp/tokens.json")
    d = config.to_dict()
    assert d["token_persist_path"] == "/tmp/tokens.json"


def test_config_from_dict_with_persist_path() -> None:
    config, warnings = EcosystemConfig.from_dict(
        {
            "token_persist_path": "/data/tokens.json",
        }
    )
    assert config.token_persist_path == "/data/tokens.json"


def test_config_from_dict_persist_path_none() -> None:
    config, _ = EcosystemConfig.from_dict({})
    assert config.token_persist_path is None
