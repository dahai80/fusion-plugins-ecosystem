"""Caveman token 压缩插件（内置、默认挂载）。

对应 PRD：
- 内置 caveman 等 token 压缩插件，默认挂载给 Claude 会话，降低 token 消耗
- 代码上下文压缩分类

设计原则：
- 插件入口接受 (desk_context, params) 并返回 dict 结果
- 通过 PluginManifest 声明能力，由 ClaudeSkillAdapter / MCPExporter 自动转
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


# ── 压缩策略 ──
# 简化的 caveman 风格压缩：去除冗余空白/注释，合并重复行
_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*/)")
_URL_PREFIX_RE = re.compile(r"(https?://)")
_MULTI_BLANK_RE = re.compile(r"\n\s*\n\s*\n+")


def _compress_text(text: str, keep_comments: bool = False) -> str:
    """对文本做 caveman 风格压缩。

    Args:
        text: 原始文本
        keep_comments: 是否保留注释行

    Returns:
        压缩后的文本
    """
    lines: list[str] = []
    prev_blank = False
    for line in text.splitlines():
        stripped = line.strip()
        # 注释处理：跳过注释行，但保留含 URL 的行
        if not keep_comments and _COMMENT_RE.match(line):
            if not _URL_PREFIX_RE.search(line):
                continue
        # 多个连续空行合并为一个
        if not stripped:
            if prev_blank:
                continue
            prev_blank = True
            lines.append("")
            continue
        prev_blank = False
        # 行内多空格合并
        lines.append(re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(lines).rstrip()


def caveman_compress(
    desk: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """插件入口：对输入文本做 token 压缩。

    Args (params):
        text: 待压缩文本
        keep_comments: 是否保留注释（默认 False）
        strategy: 压缩策略（暂只支持 "caveman"）

    Returns:
        {
            "compressed": str,            # 压缩结果
            "original_chars": int,        # 原始字符数
            "compressed_chars": int,      # 压缩后字符数
            "ratio": float,               # 压缩率（0-1）
            "strategy": str,
        }
    """
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
    ratio = (
        compressed_chars / original_chars if original_chars > 0 else 0.0
    )

    # 通过 desk 统一日志（对应 PRD「插件日志统一汇总到 Desk」）
    if desk is not None:
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


# ── 插件清单 ──
CAVEMAN_MANIFEST = PluginManifest(
    id="caveman_compress",
    name="Caveman Token Compressor",
    version="0.1.0",
    category=PluginCategory.CONTEXT_COMPRESS,
    description=(
        "对输入文本做 caveman 风格 token 压缩："
        "去除冗余空白/注释，合并重复空行，降低 Claude 会话 token 消耗。"
    ),
    capabilities=(
        PluginCapability.MCP_TOOL,
        PluginCapability.CLAUDE_SKILL,
    ),
    params=(
        PluginParam(
            name="text",
            type=PluginParamType.STRING,
            description="待压缩的文本内容",
            required=True,
        ),
        PluginParam(
            name="keep_comments",
            type=PluginParamType.BOOL,
            description="是否保留注释行（默认 False，即移除注释）",
            required=False,
            default=False,
        ),
        PluginParam(
            name="strategy",
            type=PluginParamType.STRING,
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
