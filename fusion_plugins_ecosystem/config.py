# Ecosystem config panel (one-click toggles).
# Maps to PRD "Claude integration switch (ecosystem config panel)": the
# ecosystem backend exposes one-click toggles for Claude MCP exposure,
# automatic plugin-to-Claude-Skill export, and subagent auto-destroy on
# timeout.

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fusion_core.config import service_data_dir

logger = logging.getLogger(__name__)

# issue #38: single-point data dir resolution (fusion-core #31, config-conventions §1).
# env override: FUSION_PLUGINS_ECOSYSTEM_DATA_DIR; default locks the existing
# deployment path ~/.fusion-plugins-ecosystem (fm- key / key_salt / vault
# already persisted there, not migrated).
DATA_DIR = service_data_dir(
    "plugins-ecosystem",
    default=Path.home() / ".fusion-plugins-ecosystem",
)


# Ecosystem config (persisted to the Desk config center).
# All toggles default to True, matching the PRD goal of "fully native
# Claude compatibility".
@dataclass
class EcosystemConfig:
    # ── Claude full chain ──
    # Expose Claude MCP externally
    enable_claude_mcp: bool = True
    # Auto-export plugins as Claude Skills
    auto_export_claude_skill: bool = True
    # Auto-destroy subagents on timeout (PRD "subagent timeout auto-destroy")
    subagent_timeout_destroy: bool = True
    # Mount the token compression plugin to Claude sessions by default
    default_mount_compressor: bool = True

    # ── Volcengine Claude Coding Plan ──
    # Support Volcengine Claude Coding Plan credential auth
    enable_volcengine_claude_plan: bool = True

    # ── Pain-point companions ──
    # Aggregate plugin logs into Desk
    unified_log_to_desk: bool = True
    # Mixed quantization support (fusion-mlx local inference as Claude
    # vision/image generation backend)
    enable_mixed_quantization: bool = True

    # ── Timeout thresholds ──
    # Default subagent timeout in seconds
    subagent_timeout_seconds: int = 600
    # Heartbeat stale threshold (seconds)
    heartbeat_stale_seconds: int = 120
    # Max automatic restarts
    max_auto_restart: int = 3

    # ── MCP service ──
    # MCP transport type
    mcp_transport: str = "stdio"
    # MCP listen host
    mcp_host: str = "127.0.0.1"
    # MCP listen port (0 = auto)
    mcp_port: int = 0

    # ── Sandbox ──
    # Default sandbox mode
    sandbox_default_mode: str = "inline"

    # ── Studio semantic alignment fields (A3 fix: these were previously
    # mis-mapped to unrelated fields like mcp_transport/mcp_port; now the
    # real backend fields are filled in, Studio keys 1:1 aligned) ──
    # Log level (DEBUG/INFO/WARNING/ERROR), applied to logging at startup
    log_level: str = "INFO"
    # Global VRAM cap (MB), synced to DeskRuntime.vram_total_mb at startup
    vram_limit_mb: int = 0
    # Max concurrently executing plugins (inline thread pool concurrency gate)
    max_concurrent_plugins: int = 16

    # ── Metering ──
    # Max token records kept
    max_token_records: int = 10000
    # Token record persistence path (None = in-memory only)
    token_persist_path: str | None = None

    # ── Connector framework ──
    # Enable the external MCP connector framework (connector pack install /
    # tool bridging)
    enable_connector_framework: bool = True
    # Connector registry persistence path (None = in-memory only)
    connector_persist_path: str | None = None
    # Connector marketplace index (local JSON path or HTTPS URL,
    # None = builtin catalog only)
    connector_marketplace_index: str | None = None

    # ── Change callbacks ──
    _observers: list[Callable[[str, Any, Any], None]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, object]:
        # Serialize to a dict (for Desk config center persistence).
        return {
            "enable_claude_mcp": self.enable_claude_mcp,
            "auto_export_claude_skill": self.auto_export_claude_skill,
            "subagent_timeout_destroy": self.subagent_timeout_destroy,
            "default_mount_compressor": self.default_mount_compressor,
            "enable_volcengine_claude_plan": self.enable_volcengine_claude_plan,
            "unified_log_to_desk": self.unified_log_to_desk,
            "enable_mixed_quantization": self.enable_mixed_quantization,
            "subagent_timeout_seconds": self.subagent_timeout_seconds,
            "heartbeat_stale_seconds": self.heartbeat_stale_seconds,
            "max_auto_restart": self.max_auto_restart,
            "mcp_transport": self.mcp_transport,
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "sandbox_default_mode": self.sandbox_default_mode,
            "log_level": self.log_level,
            "vram_limit_mb": self.vram_limit_mb,
            "max_concurrent_plugins": self.max_concurrent_plugins,
            "max_token_records": self.max_token_records,
            "token_persist_path": self.token_persist_path,
            "enable_connector_framework": self.enable_connector_framework,
            "connector_persist_path": self.connector_persist_path,
            "connector_marketplace_index": self.connector_marketplace_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> tuple[EcosystemConfig, list[str]]:
        # Deserialize from a dict, returning (config, warnings).
        defaults = cls().to_dict()
        merged = {**defaults, **data}
        warnings: list[str] = []

        def _safe_bool(key: str) -> bool:
            val = merged[key]
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            warnings.append(f"config.{key}: type mismatch {type(val).__name__}, using default")
            return defaults[key]  # type: ignore[return-value]

        def _safe_int(key: str, min_val: int = 0, max_val: int = 100000) -> int:
            val = merged[key]
            try:
                result = int(val)  # type: ignore[arg-type]
                if result < min_val or result > max_val:
                    warnings.append(f"config.{key}: {result} out of range [{min_val},{max_val}]")
                    return defaults[key]  # type: ignore[return-value]
                return result
            except (ValueError, TypeError):
                warnings.append(f"config.{key}: type mismatch {type(val).__name__}, using default")
                return defaults[key]  # type: ignore[return-value]

        def _safe_str(key: str, allowed: tuple[str, ...] | None = None) -> str:
            val = merged[key]
            if isinstance(val, str):
                if allowed and val not in allowed:
                    warnings.append(f"config.{key}: {val!r} not in {allowed}, using default")
                    return defaults[key]  # type: ignore[return-value]
                return val
            warnings.append(f"config.{key}: type mismatch {type(val).__name__}, using default")
            return defaults[key]  # type: ignore[return-value]

        config = cls(
            enable_claude_mcp=_safe_bool("enable_claude_mcp"),
            auto_export_claude_skill=_safe_bool("auto_export_claude_skill"),
            subagent_timeout_destroy=_safe_bool("subagent_timeout_destroy"),
            default_mount_compressor=_safe_bool("default_mount_compressor"),
            enable_volcengine_claude_plan=_safe_bool("enable_volcengine_claude_plan"),
            unified_log_to_desk=_safe_bool("unified_log_to_desk"),
            enable_mixed_quantization=_safe_bool("enable_mixed_quantization"),
            subagent_timeout_seconds=_safe_int("subagent_timeout_seconds", 1),
            heartbeat_stale_seconds=_safe_int("heartbeat_stale_seconds", 1),
            max_auto_restart=_safe_int("max_auto_restart", 0, 100),
            mcp_transport=_safe_str("mcp_transport", ("stdio", "sse", "http")),
            mcp_host=_safe_str("mcp_host"),
            mcp_port=_safe_int("mcp_port", 0, 65535),
            sandbox_default_mode=_safe_str("sandbox_default_mode", ("inline", "process")),
            log_level=_safe_str("log_level", ("DEBUG", "INFO", "WARNING", "ERROR")),
            vram_limit_mb=_safe_int("vram_limit_mb", 0, 65535),
            max_concurrent_plugins=_safe_int("max_concurrent_plugins", 1, 1024),
            max_token_records=_safe_int("max_token_records", 100),
            token_persist_path=merged.get("token_persist_path"),
            enable_connector_framework=_safe_bool("enable_connector_framework"),
            connector_persist_path=merged.get("connector_persist_path"),
            connector_marketplace_index=merged.get("connector_marketplace_index"),
        )

        for w in warnings:
            logger.warning("config: %s", w)

        return config, warnings

    def validate(self) -> list[str]:
        # Return validation errors (empty list means all valid).
        errors: list[str] = []
        if self.subagent_timeout_seconds <= 0:
            errors.append("subagent_timeout_seconds must be > 0")
        if self.heartbeat_stale_seconds <= 0:
            errors.append("heartbeat_stale_seconds must be > 0")
        if self.max_auto_restart < 0:
            errors.append("max_auto_restart must be >= 0")
        if self.mcp_transport not in ("stdio", "sse", "http"):
            errors.append(f"mcp_transport must be stdio/sse/http, got {self.mcp_transport!r}")
        if self.mcp_port < 0 or self.mcp_port > 65535:
            errors.append(f"mcp_port must be 0-65535, got {self.mcp_port}")
        return errors

    def add_observer(self, callback: Callable[[str, Any, Any], None]) -> None:
        # Register a config change callback.
        self._observers.append(callback)

    def _notify_change(self, key: str, old_value: Any, new_value: Any) -> None:
        # Notify observers of a config change.
        for obs in self._observers:
            try:
                obs(key, old_value, new_value)
            except Exception as exc:
                logger.warning("config: observer exception: %s", exc)
