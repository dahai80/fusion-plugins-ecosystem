"""EcosystemConfig 测试。"""

from __future__ import annotations


from fusion_plugins_ecosystem.config import EcosystemConfig


def test_defaults_all_true() -> None:
    config = EcosystemConfig()
    assert config.enable_claude_mcp is True
    assert config.auto_export_claude_skill is True
    assert config.subagent_timeout_destroy is True
    assert config.default_mount_compressor is True
    assert config.enable_volcengine_claude_plan is True
    assert config.unified_log_to_desk is True
    assert config.enable_mixed_quantization is True


def test_defaults_timeout_values() -> None:
    config = EcosystemConfig()
    assert config.subagent_timeout_seconds == 600
    assert config.heartbeat_stale_seconds == 120
    assert config.max_auto_restart == 3


def test_to_dict_contains_all_fields() -> None:
    config = EcosystemConfig()
    d = config.to_dict()
    assert "enable_claude_mcp" in d
    assert "auto_export_claude_skill" in d
    assert "subagent_timeout_destroy" in d
    assert "default_mount_compressor" in d
    assert "enable_volcengine_claude_plan" in d
    assert "unified_log_to_desk" in d
    assert "enable_mixed_quantization" in d
    assert "subagent_timeout_seconds" in d
    assert "heartbeat_stale_seconds" in d
    assert "max_auto_restart" in d


def test_roundtrip_preserves_all_values() -> None:
    config = EcosystemConfig()
    config.enable_claude_mcp = False
    config.subagent_timeout_seconds = 300
    config.max_auto_restart = 5
    d = config.to_dict()
    restored, warnings = EcosystemConfig.from_dict(d)
    assert restored.enable_claude_mcp is False
    assert restored.subagent_timeout_seconds == 300
    assert restored.max_auto_restart == 5
    assert warnings == []


def test_from_dict_empty_uses_defaults() -> None:
    restored, warnings = EcosystemConfig.from_dict({})
    assert restored.enable_claude_mcp is True
    assert restored.subagent_timeout_seconds == 600
    assert warnings == []


def test_from_dict_partial_override() -> None:
    restored, warnings = EcosystemConfig.from_dict({"enable_claude_mcp": False})
    assert restored.enable_claude_mcp is False
    assert restored.auto_export_claude_skill is True
    assert warnings == []


def test_to_dict_returns_dict_type() -> None:
    config = EcosystemConfig()
    d = config.to_dict()
    assert isinstance(d, dict)


def test_from_dict_unknown_key_ignored() -> None:
    restored, warnings = EcosystemConfig.from_dict(
        {"unknown_key": "value", "enable_claude_mcp": False}
    )
    assert restored.enable_claude_mcp is False


def test_disabled_flags_persist() -> None:
    config = EcosystemConfig()
    config.enable_claude_mcp = False
    config.auto_export_claude_skill = False
    config.subagent_timeout_destroy = False
    config.default_mount_compressor = False
    config.enable_volcengine_claude_plan = False
    config.unified_log_to_desk = False
    config.enable_mixed_quantization = False
    d = config.to_dict()
    restored, warnings = EcosystemConfig.from_dict(d)
    assert restored.enable_claude_mcp is False
    assert restored.auto_export_claude_skill is False
    assert restored.subagent_timeout_destroy is False
    assert restored.default_mount_compressor is False
    assert restored.enable_volcengine_claude_plan is False
    assert restored.unified_log_to_desk is False
    assert restored.enable_mixed_quantization is False
    assert warnings == []


# ── validate ──


def test_validate_valid_config() -> None:
    config = EcosystemConfig()
    assert config.validate() == []


def test_validate_zero_timeout() -> None:
    config = EcosystemConfig(subagent_timeout_seconds=0)
    errors = config.validate()
    assert any("subagent_timeout_seconds" in e for e in errors)


def test_validate_negative_timeout() -> None:
    config = EcosystemConfig(subagent_timeout_seconds=-1)
    errors = config.validate()
    assert any("subagent_timeout_seconds" in e for e in errors)


def test_validate_zero_heartbeat() -> None:
    config = EcosystemConfig(heartbeat_stale_seconds=0)
    errors = config.validate()
    assert any("heartbeat_stale_seconds" in e for e in errors)


def test_validate_negative_max_restart() -> None:
    config = EcosystemConfig(max_auto_restart=-1)
    errors = config.validate()
    assert any("max_auto_restart" in e for e in errors)


def test_validate_invalid_transport() -> None:
    config = EcosystemConfig(mcp_transport="websocket")
    errors = config.validate()
    assert any("mcp_transport" in e for e in errors)


def test_validate_port_out_of_range() -> None:
    config = EcosystemConfig(mcp_port=70000)
    errors = config.validate()
    assert any("mcp_port" in e for e in errors)


def test_validate_port_negative() -> None:
    config = EcosystemConfig(mcp_port=-1)
    errors = config.validate()
    assert any("mcp_port" in e for e in errors)


# ── from_dict edge cases ──


def test_from_dict_bool_string_true() -> None:
    restored, _ = EcosystemConfig.from_dict({"enable_claude_mcp": "true"})
    assert restored.enable_claude_mcp is True


def test_from_dict_bool_string_yes() -> None:
    restored, _ = EcosystemConfig.from_dict({"enable_claude_mcp": "yes"})
    assert restored.enable_claude_mcp is True


def test_from_dict_bool_string_1() -> None:
    restored, _ = EcosystemConfig.from_dict({"enable_claude_mcp": "1"})
    assert restored.enable_claude_mcp is True


def test_from_dict_bool_string_false() -> None:
    restored, _ = EcosystemConfig.from_dict({"enable_claude_mcp": "false"})
    assert restored.enable_claude_mcp is False


def test_from_dict_bool_invalid_type_warns() -> None:
    restored, warnings = EcosystemConfig.from_dict({"enable_claude_mcp": 42})
    assert len(warnings) > 0
    assert restored.enable_claude_mcp is True  # default


def test_from_dict_int_out_of_range_warns() -> None:
    restored, warnings = EcosystemConfig.from_dict({"subagent_timeout_seconds": 999999})
    assert len(warnings) > 0


def test_from_dict_int_invalid_type_warns() -> None:
    restored, warnings = EcosystemConfig.from_dict(
        {"subagent_timeout_seconds": "not_a_number"}
    )
    assert len(warnings) > 0


def test_from_dict_transport_invalid_warns() -> None:
    restored, warnings = EcosystemConfig.from_dict({"mcp_transport": "websocket"})
    assert len(warnings) > 0
    assert restored.mcp_transport == "stdio"


def test_from_dict_sandbox_mode_invalid_warns() -> None:
    restored, warnings = EcosystemConfig.from_dict({"sandbox_default_mode": "docker"})
    assert len(warnings) > 0
    assert restored.sandbox_default_mode == "inline"


def test_from_dict_mcp_fields() -> None:
    restored, _ = EcosystemConfig.from_dict(
        {
            "mcp_transport": "sse",
            "mcp_host": "0.0.0.0",
            "mcp_port": 8080,
        }
    )
    assert restored.mcp_transport == "sse"
    assert restored.mcp_host == "0.0.0.0"
    assert restored.mcp_port == 8080


def test_from_dict_sandbox_and_token_fields() -> None:
    restored, _ = EcosystemConfig.from_dict(
        {
            "sandbox_default_mode": "process",
            "max_token_records": 5000,
            "token_persist_path": "/tmp/tokens.json",
        }
    )
    assert restored.sandbox_default_mode == "process"
    assert restored.max_token_records == 5000
    assert restored.token_persist_path == "/tmp/tokens.json"


def test_from_dict_token_persist_path_none() -> None:
    restored, _ = EcosystemConfig.from_dict({"token_persist_path": None})
    assert restored.token_persist_path is None


# ── observer ──


def test_add_observer_and_notify() -> None:
    changes: list[tuple[str, object, object]] = []
    config = EcosystemConfig()
    config.add_observer(lambda k, o, n: changes.append((k, o, n)))
    config._notify_change("enable_claude_mcp", True, False)
    assert changes == [("enable_claude_mcp", True, False)]


def test_observer_exception_does_not_propagate() -> None:
    config = EcosystemConfig()
    config.add_observer(lambda k, o, n: 1 / 0)
    config._notify_change("test", "a", "b")  # should not raise


def test_multiple_observers_all_called() -> None:
    results: list[str] = []
    config = EcosystemConfig()
    config.add_observer(lambda k, o, n: results.append("first"))
    config.add_observer(lambda k, o, n: results.append("second"))
    config._notify_change("x", 1, 2)
    assert results == ["first", "second"]
