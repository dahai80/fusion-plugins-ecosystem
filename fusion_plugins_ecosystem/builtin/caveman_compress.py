# Caveman token compression plugin (builtin, default-mounted).
# Maps to PRD:
# - Builtin caveman-style token compression plugins mounted to Claude
#   sessions by default to reduce token consumption
# - Code context compression category
# Design principles:
# - Plugin entry points accept (desk, params) and return a dict result
# - Capabilities are declared via PluginManifest and auto-converted by
#   ClaudeSkillAdapter / MCPExporter

from __future__ import annotations

import logging
import re
from typing import Any

from fusion_plugins_ecosystem import __version__ as _PKG_VERSION
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


# ── Compression strategies ──
# Simplified caveman-style compression: strip redundant whitespace/comments,
# merge duplicate lines
_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*/)")
_URL_PREFIX_RE = re.compile(r"(https?://)")
_MULTI_BLANK_RE = re.compile(r"\n\s*\n\s*\n+")


def _compress_text(text: str, keep_comments: bool = False) -> str:
    # Apply caveman-style compression to text.
    # text: original text; keep_comments: whether to keep comment lines.
    # Returns the compressed text.
    lines: list[str] = []
    prev_blank = False
    for line in text.splitlines():
        stripped = line.strip()
        # Comment handling: skip comment lines, but keep lines containing URLs
        if not keep_comments and _COMMENT_RE.match(line):
            if not _URL_PREFIX_RE.search(line):
                continue
        # Collapse runs of consecutive blank lines into one
        if not stripped:
            if prev_blank:
                continue
            prev_blank = True
            lines.append("")
            continue
        prev_blank = False
        # Collapse multiple in-line spaces
        lines.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(lines).rstrip()


def caveman_compress(desk: Any, params: dict[str, Any]) -> dict[str, Any]:
    # Plugin entry point: compress the input text to save tokens.
    # Params:
    #     text: text to compress
    #     keep_comments: whether to keep comments (default False)
    #     strategy: compression strategy (only "caveman" supported for now)
    # Returns:
    #     {
    #         "compressed": str,            # compression result
    #         "original_chars": int,        # original char count
    #         "compressed_chars": int,      # compressed char count
    #         "ratio": float,               # compression ratio (0-1)
    #         "strategy": str,
    #     }
    text: str = params.get("text", "")
    keep_comments_raw = params.get("keep_comments", False)
    keep_comments: bool = keep_comments_raw is True or keep_comments_raw == "true"
    strategy: str = params.get("strategy", "caveman")

    if not text:
        return {
            "compressed": "",
            "original_chars": 0,
            "compressed_chars": 0,
            "ratio": 0.0,
            "strategy": strategy,
        }

    original_chars = len(text)
    compressed = _compress_text(text, keep_comments=keep_comments)
    compressed_chars = len(compressed)
    ratio = compressed_chars / original_chars if original_chars > 0 else 0.0

    # Unified logging via desk (PRD "plugin logs aggregated into Desk")
    if desk is not None:
        # i18n: user-facing
        desk.log(
            "caveman_compress",
            "INFO",
            "压缩完成",
            original=original_chars,
            compressed=compressed_chars,
            ratio=ratio,
        )

    return {
        "compressed": compressed,
        "original_chars": original_chars,
        "compressed_chars": compressed_chars,
        "ratio": ratio,
        "strategy": strategy,
    }


# ── Plugin manifest ──
CAVEMAN_MANIFEST = PluginManifest(
    id="caveman_compress",
    name="Caveman Token Compressor",
    version=_PKG_VERSION,
    category=PluginCategory.CONTEXT_COMPRESS,
    # i18n: user-facing
    description=(
        "对输入文本做 caveman 风格 token 压缩：去除冗余空白/注释，合并重复空行，降低 Claude 会话 token 消耗。"
    ),
    capabilities=(
        PluginCapability.MCP_TOOL,
        PluginCapability.CLAUDE_SKILL,
    ),
    params=(
        PluginParam(
            name="text",
            type=PluginParamType.STRING,
            # i18n: user-facing
            description="待压缩的文本内容",
            required=True,
        ),
        PluginParam(
            name="keep_comments",
            type=PluginParamType.BOOL,
            # i18n: user-facing
            description="是否保留注释行（默认 False，即移除注释）",
            required=False,
            default=False,
        ),
        PluginParam(
            name="strategy",
            type=PluginParamType.STRING,
            # i18n: user-facing
            description="压缩策略",
            required=False,
            default="caveman",
            enum=("caveman",),
        ),
    ),
    entry_point=caveman_compress,
    default_mounted=True,
    timeout_seconds=120,
    vram_mb=0,
)
