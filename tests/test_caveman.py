"""Caveman 压缩插件测试。"""

from __future__ import annotations

from unittest.mock import MagicMock


from fusion_plugins_ecosystem.builtin.caveman_compress import (
    CAVEMAN_MANIFEST,
    _compress_text,
    caveman_compress,
)


# ── _compress_text ──


def test_compress_text_removes_hash_comments() -> None:
    text = "# comment\ncode\n# another\nmore"
    result = _compress_text(text)
    assert "# comment" not in result
    assert "# another" not in result
    assert "code" in result
    assert "more" in result


def test_compress_text_removes_slash_comments() -> None:
    text = "// comment\ncode\n/* block */\nmore"
    result = _compress_text(text)
    assert "// comment" not in result
    assert "/* block */" not in result


def test_compress_text_keep_comments_true() -> None:
    text = "# comment\ncode"
    result = _compress_text(text, keep_comments=True)
    assert "# comment" in result


def test_compress_text_collapses_blank_lines() -> None:
    text = "a\n\n\n\nb"
    result = _compress_text(text)
    assert "\n\n\n" not in result


def test_compress_text_preserves_single_blank() -> None:
    text = "a\n\nb"
    result = _compress_text(text)
    assert "a\n\nb" == result


def test_compress_text_collapses_inline_spaces() -> None:
    text = "code    with    spaces"
    result = _compress_text(text)
    assert "code with spaces" == result


def test_compress_text_empty() -> None:
    assert _compress_text("") == ""


def test_compress_text_only_comments() -> None:
    text = "# comment 1\n# comment 2"
    result = _compress_text(text)
    assert result == ""


def test_compress_text_only_blanks() -> None:
    text = "\n\n\n"
    result = _compress_text(text)
    assert result == ""


def test_compress_text_strips_trailing_whitespace() -> None:
    text = "code\n\n\n"
    result = _compress_text(text)
    assert result == "code"


# ── caveman_compress entry point ──


def test_caveman_compress_basic() -> None:
    text = "# c\ncode"
    result = caveman_compress(None, {"text": text})
    assert result["compressed"] == "code"
    assert result["original_chars"] == len(text)
    assert result["compressed_chars"] == 4
    assert result["ratio"] < 1.0
    assert result["strategy"] == "caveman"


def test_caveman_compress_empty_text() -> None:
    result = caveman_compress(None, {"text": ""})
    assert result["compressed"] == ""
    assert result["original_chars"] == 0
    assert result["compressed_chars"] == 0
    assert result["ratio"] == 0.0


def test_caveman_compress_keep_comments_param() -> None:
    result = caveman_compress(
        None,
        {"text": "# comment\ncode", "keep_comments": True},
    )
    assert "# comment" in result["compressed"]


def test_caveman_compress_strategy_param() -> None:
    result = caveman_compress(None, {"text": "x", "strategy": "caveman"})
    assert result["strategy"] == "caveman"


def test_caveman_compress_with_desk_logs() -> None:
    desk_mock = MagicMock()
    caveman_compress(desk_mock, {"text": "hello"})
    assert desk_mock.log.called
    call_args = desk_mock.log.call_args
    assert call_args[0][0] == "caveman_compress"
    assert call_args[0][1] == "INFO"
    assert "压缩完成" in call_args[0][2]


def test_caveman_compress_ratio_calculation() -> None:
    text = "a" * 100
    result = caveman_compress(None, {"text": text})
    # 100 个 a，压缩后还是 100 个 a
    assert result["original_chars"] == 100
    assert result["compressed_chars"] == 100
    assert result["ratio"] == 1.0


def test_caveman_compress_reduces_size_with_comments() -> None:
    text = "# " + "x" * 50 + "\n" + "actual code"
    result = caveman_compress(None, {"text": text})
    assert result["compressed_chars"] < result["original_chars"]
    assert result["ratio"] < 1.0


# ── CAVEMAN_MANIFEST ──


def test_caveman_manifest_id() -> None:
    assert CAVEMAN_MANIFEST.id == "caveman_compress"


def test_caveman_manifest_default_mounted() -> None:
    assert CAVEMAN_MANIFEST.default_mounted is True


def test_caveman_manifest_category() -> None:
    from fusion_plugins_ecosystem.registry import PluginCategory

    assert CAVEMAN_MANIFEST.category == PluginCategory.CONTEXT_COMPRESS


def test_caveman_manifest_capabilities() -> None:
    from fusion_plugins_ecosystem.registry import PluginCapability

    caps = CAVEMAN_MANIFEST.capabilities
    assert PluginCapability.MCP_TOOL in caps
    assert PluginCapability.CLAUDE_SKILL in caps
    # LONG_TASK removed: caveman is a fast sync operation


def test_caveman_manifest_params() -> None:
    param_names = [p.name for p in CAVEMAN_MANIFEST.params]
    assert "text" in param_names
    assert "keep_comments" in param_names
    assert "strategy" in param_names


def test_caveman_manifest_text_required() -> None:
    text_param = next(p for p in CAVEMAN_MANIFEST.params if p.name == "text")
    assert text_param.required is True


def test_caveman_manifest_strategy_enum() -> None:
    strategy_param = next(p for p in CAVEMAN_MANIFEST.params if p.name == "strategy")
    assert strategy_param.enum == ("caveman",)


def test_caveman_manifest_timeout() -> None:
    assert CAVEMAN_MANIFEST.timeout_seconds == 120


def test_caveman_manifest_vram_zero() -> None:
    assert CAVEMAN_MANIFEST.vram_mb == 0


def test_caveman_manifest_entry_point_callable() -> None:
    assert callable(CAVEMAN_MANIFEST.entry_point)
