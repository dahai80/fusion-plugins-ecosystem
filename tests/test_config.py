"""EcosystemConfig 测试。"""

from __future__ import annotations

import pytest

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
    restored = EcosystemConfig.from_dict(d)
    assert restored.enable_claude_mcp is False
    assert restored.subagent_timeout_seconds == 300
    assert restored.max_auto_restart == 5


def test_from_dict_empty_uses_defaults() -> None:
    restored = EcosystemConfig.from_dict({})
    assert restored.enable_claude_mcp is True
    assert restored.subagent_timeout_seconds == 600


def test_from_dict_partial_override() -> None:
    restored = EcosystemConfig.from_dict({"enable_claude_mcp": False})
    assert restored.enable_claude_mcp is False
    # 其他字段保持默认
    assert restored.auto_export_claude_skill is True


def test_to_dict_returns_dict_type() -> None:
    config = EcosystemConfig()
    d = config.to_dict()
    assert isinstance(d, dict)


def test_from_dict_unknown_key_ignored() -> None:
    restored = EcosystemConfig.from_dict(
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
    restored = EcosystemConfig.from_dict(d)
    assert restored.enable_claude_mcp is False
    assert restored.auto_export_claude_skill is False
    assert restored.subagent_timeout_destroy is False
    assert restored.default_mount_compressor is False
    assert restored.enable_volcengine_claude_plan is False
    assert restored.unified_log_to_desk is False
    assert restored.enable_mixed_quantization is False
