# fusion-cowork runtime adapter layer.
# Wraps the real fusion-cowork runtime handles:
# - NodeRegistry: node type registry
# - TaskScheduler: scheduled task scheduler
# - FusionMLXClient: local MLX inference client
# - WorkflowEngine: workflow engine
# The plugin ecosystem reaches cowork's underlying abstractions through
# DeskRuntime; every call is relayed via this layer so tests can inject
# a fake runtime.

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Log ring buffer cap (avoid unbounded growth)
_LOG_BUFFER_MAX = 2000

# API key encryption prefix (R6: no plaintext keys at rest in config_center)
_KEY_ENC_PREFIX = "enc:"


# Derive a machine-local key for API key symmetric encryption (Fernet).
# Key material = hostname + current user + env salt FUSION_PLUGIN_KEY_SALT.
# Protects against plaintext key leakage from config files under a
# single-host threat model; not decryptable across machines/users.
def _derive_key() -> bytes:
    # issue #36: default salt literal removed (config-conventions §3) — unset
    # means empty, no silent fallback to a guessable constant. Prod missing
    # salt fail-fasts; dev empty salt is allowed but warns loudly.
    salt = os.environ.get("FUSION_PLUGIN_KEY_SALT", "")
    if not salt:
        if os.environ.get("FUSION_PROFILE", "dev") == "prod":
            raise RuntimeError(
                "FUSION_PLUGIN_KEY_SALT not set — refusing default salt in prod. "
                "Generate one via `openssl rand -hex 32` and export it for the "
                "service user. See architecture/config-conventions.md §3."
            )
        logger.warning(
            "desk_runtime: FUSION_PLUGIN_KEY_SALT not set, key derivation uses an "
            "empty salt (lenient in dev); must be set in production "
            "(architecture/config-conventions.md §3)"
        )
    material = f"{salt}|{socket.gethostname()}|{os.getuid()}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


# Resolve the real path safely; fall back to normpath if realpath fails (never raises).
def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


# Production strict mode: with FUSION_PLUGIN_STRICT_ENCRYPTION=1, key
# encryption failure is a hard error — never fall back to plaintext at rest
# (violates R6 design intent). Tests/offline default to lenient fallback.
def _strict_encryption() -> bool:
    return os.environ.get("FUSION_PLUGIN_STRICT_ENCRYPTION", "") == "1"


# Encrypt an API key, returning ciphertext with the enc: prefix.
# P2-4: in strict mode, missing cryptography or encryption failure raises
# instead of falling back to plaintext; lenient mode (default, for
# tests/offline) falls back to plaintext with a warning.
def _encrypt_key(plaintext: str) -> str:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        if _strict_encryption():
            raise RuntimeError(
                "desk_runtime: strict mode with cryptography not installed, "
                "refusing to store API key in plaintext"
            ) from None
        logger.warning("desk_runtime: cryptography not installed, storing API key in plaintext")
        return plaintext
    try:
        return _KEY_ENC_PREFIX + Fernet(_derive_key()).encrypt(
            plaintext.encode("utf-8")
        ).decode("ascii")
    except Exception as exc:
        if _strict_encryption():
            raise RuntimeError(
                f"desk_runtime: strict mode API key encryption failed, refusing plaintext fallback: {exc}"
            ) from exc
        logger.warning("desk_runtime: API key encryption failed, falling back to plaintext: %s", exc)
        return plaintext


# Decrypt an API key; values without the enc: prefix are treated as legacy
# plaintext and returned as-is (backward compatible).
def _decrypt_key(stored: str | None) -> str | None:
    if stored is None:
        return None
    if not stored.startswith(_KEY_ENC_PREFIX):
        return stored
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("desk_runtime: cryptography not installed, cannot decrypt API key")
        return None
    try:
        return (
            Fernet(_derive_key())
            .decrypt(stored[len(_KEY_ENC_PREFIX) :].encode("ascii"))
            .decode("utf-8")
        )
    except Exception as exc:
        logger.warning("desk_runtime: API key decryption failed: %s", exc)
        return None


FUSION_COWORK_AVAILABLE = False
try:
    import fusion_cowork  # noqa: F401

    FUSION_COWORK_AVAILABLE = True
except ImportError:
    pass


@dataclass
# fusion-cowork runtime handle wrapper.
# Injected by fusion-cowork at startup; the plugin ecosystem reaches it via
# PluginRegistry.desk. When any field is None, the corresponding capability
# degrades to a no-op (test or offline scenarios).
class DeskRuntime:
    # fusion-cowork node registry (fusion_cowork.NodeRegistry)
    node_registry: Any | None = None
    # fusion-cowork task scheduler (fusion_cowork.TaskScheduler)
    task_scheduler: Any | None = None
    # fusion-cowork MLX client (fusion_cowork.FusionMLXClient)
    mlx_client: Any | None = None
    # fusion-cowork workflow engine (fusion_cowork.WorkflowEngine)
    workflow_engine: Any | None = None
    # fusion-cowork logger
    desk_logger: logging.Logger | None = None
    # Config center handle (dict-like, supports get/set)
    config_center: Any | None = None
    # MCP gateway port (allocated by cowork)
    mcp_gateway_port: int | None = None
    # Registered plugin permission table {plugin_id: {allowed_paths: [...], capabilities: [...]}}
    plugin_permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # vRAM allocation ledger {plugin_id: mb}
    vram_allocations: dict[str, int] = field(default_factory=dict)
    # Total vRAM budget (MB); 0 means unlimited
    vram_total_mb: int = 0
    # Set of registered plugin IDs
    registered_plugin_ids: set[str] = field(default_factory=set)
    # Log ring buffer (consumers query historical logs from it)
    log_entries: deque = field(default_factory=lambda: deque(maxlen=_LOG_BUFFER_MAX))
    # Log auto-increment counter (generates log entry ids)
    _log_counter: int = field(default=0)
    # vRAM operation lock (thread safety): acquire_vram/release_vram are sync
    # methods called on sync load/unload paths; asyncio.Lock cannot be awaited
    # in sync code (P2-2)
    _vram_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Log buffer write lock: _log_counter increment + append must be atomic to
    # avoid duplicate ids / dropped entries under concurrency
    _log_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # R5: MCP session state + rate limit timestamps shared across handlers.
    # When multiple MCPHandlers share one DeskRuntime, rate limiting/sessions
    # use desk as the single source of truth, avoiding per-handler counting
    # that would multiply the rate limit N-fold with N handlers.
    _mcp_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    _mcp_call_timestamps: dict[str, Any] = field(default_factory=dict)
    _mcp_state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Observability metrics registry (enterprise ops): injected into modules
    # via this singleton, single source of truth
    _metrics: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not FUSION_COWORK_AVAILABLE:
            logger.info(
                "desk_runtime: fusion-cowork not installed, DeskRuntime running in degraded mode"
            )

    # Lazily initialize the MetricsRegistry singleton (avoids circular import).
    @property
    def metrics(self) -> Any:
        if self._metrics is None:
            from fusion_plugins_ecosystem.metrics import MetricsRegistry

            self._metrics = MetricsRegistry()
        return self._metrics

    # ── Cluster distributed state bridge (enterprise multi-node, step 4) ──
    # When the cluster is not enabled (FUSION_CLUSTER_ENABLED not truthy) or
    # fusion-cowork is missing, all writes are no-ops and queries return
    # empty — standalone behavior unchanged.

    # This node's id; returns None when the cluster is not enabled.
    def cluster_node_id(self) -> str | None:
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.cluster_node_id()

    # Send this node's heartbeat to cluster shared storage. No-op when the cluster is not enabled.
    def cluster_heartbeat(
        self,
        host: str = "",
        port: int = 0,
        role: str = "worker",
        vram_total_mb: int = 0,
        vram_used_mb: int = 0,
        tags: list[str] | None = None,
    ) -> None:
        from fusion_plugins_ecosystem import cluster_bridge

        cluster_bridge.cluster_heartbeat(
            host=host,
            port=port,
            role=role,
            vram_total_mb=vram_total_mb,
            vram_used_mb=vram_used_mb,
            tags=tags,
        )

    # Failover: evict stale nodes whose heartbeats timed out, cleaning up
    # their plugin state and vRAM.
    def cluster_evict_stale_nodes(self) -> list[str]:
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.cluster_evict_stale_nodes()

    # Whether the plugin is enabled on any node in the cluster. Returns False when the cluster is not enabled.
    def is_plugin_enabled_anywhere(self, plugin_id: str) -> bool:
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.is_plugin_enabled_anywhere(plugin_id)

    # List of this plugin's states across cluster nodes. Returns an empty list when the cluster is not enabled.
    def plugin_state_across_cluster(self, plugin_id: str) -> list[dict[str, Any]]:
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.plugin_state_across_cluster(plugin_id)

    # ── vRAM scheduling ──

    # Request vRAM from Desk (resize semantics, not additive).
    # Calling again with the same plugin_id replaces the previous allocation
    # instead of accumulating on top of it.
    # check-then-set is done atomically inside the lock, removing the TOCTOU
    # where concurrent requests could exceed the budget (P2-2).
    def acquire_vram(self, plugin_id: str, mb: int) -> bool:
        with self._vram_lock:
            if mb <= 0:
                self.vram_allocations.pop(plugin_id, None)
                return True
            current = self.vram_allocations.get(plugin_id, 0)
            delta = mb - current
            used = sum(self.vram_allocations.values())
            if self.vram_total_mb > 0 and used + delta > self.vram_total_mb:
                logger.warning(
                    "desk_runtime: vRAM request denied for plugin %s (%dMB exceeds budget %dMB)",
                    plugin_id,
                    mb,
                    self.vram_total_mb,
                )
                return False
            self.vram_allocations[plugin_id] = mb
            logger.info("desk_runtime: plugin %s vRAM %dMB→%dMB", plugin_id, current, mb)
            self.metrics.gauge("vram_used_mb", "allocated vRAM (MB)").set(
                sum(self.vram_allocations.values())
            )
            # Step 4: sync vRAM allocation to cluster shared storage (visible cross-node)
            from fusion_plugins_ecosystem import cluster_bridge

            cluster_bridge.record_vram(plugin_id, mb)
            return True

    # Release the vRAM held by a plugin.
    def release_vram(self, plugin_id: str) -> None:
        with self._vram_lock:
            freed = self.vram_allocations.pop(plugin_id, 0)
        if freed:
            logger.info("desk_runtime: plugin %s released %dMB vRAM", plugin_id, freed)
        self.metrics.gauge("vram_used_mb", "allocated vRAM (MB)").set(
            sum(self.vram_allocations.values())
        )
        # Step 4: sync release to cluster shared storage
        from fusion_plugins_ecosystem import cluster_bridge

        cluster_bridge.release_vram(plugin_id)

    # Return a snapshot of the current vRAM ledger.
    def vram_usage(self) -> dict[str, int]:
        return dict(self.vram_allocations)

    # ── Log collection ──

    # Unified log collection entry point (solves the "subagent has no logs" pain point).
    # All plugin logs funnel through here into the Desk full-chain log panel,
    # and are also written to the ring buffer for consumers to query history.
    def log(
        self,
        plugin_id: str,
        level: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        log_level = getattr(logging, level.upper(), logging.INFO)
        extra = f" {kwargs}" if kwargs else ""
        if self.desk_logger is not None:
            self.desk_logger.log(
                log_level,
                "[plugin=%s] %s%s",
                plugin_id,
                message,
                extra,
            )
        else:
            logger.log(
                log_level,
                "[plugin=%s] %s%s",
                plugin_id,
                message,
                extra,
            )
        # Write to the ring buffer (queried via plugins/logs.stream)
        # counter increment + append inside the lock to avoid duplicate ids
        # or dropped entries from deque scheduling under concurrency
        with self._log_lock:
            self._log_counter += 1
            self.log_entries.append(
                {
                    "id": self._log_counter,
                    "plugin_id": plugin_id,
                    "level": level.upper(),
                    "message": message,
                    "timestamp": str(int(time.time() * 1000)),
                }
            )

    # Unified infra event collection (P2-9: fixes the split dual log stream).
    # Base-layer errors from transport/jsonrpc/lifecycle/server previously went
    # only to the module logger (stderr), invisible to ops tailing
    # plugins/logs.stream — illegal state transitions, handler errors, auth
    # denials, and write-back failures were unqueryable. They funnel here into
    # the ring buffer (plugin_id="_infra") while still reaching stderr via
    # desk_logger, forming one queryable stream. component goes into kwargs
    # for per-subsystem filtering.
    def infra_log(
        self,
        component: str,
        level: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        self.log(
            "_infra", level, f"[{component}] {message}", component=component, **kwargs
        )

    # Query the log buffer (filter by plugin/level, return the most recent
    # `limit` entries).
    # Consumed by the plugins/logs.stream JSON-RPC method.
    # Reverse iteration + early termination: only the most recent `limit`
    # entries are needed, so no full list() copy + slice (P3-2).
    def get_logs(
        self,
        plugin_id: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        need_pid = plugin_id
        need_lvl = level.upper() if level is not None else None
        collected: list[dict[str, Any]] = []
        # R7: snapshot the deque on the read path then release the lock, so a
        # long-held lock cannot block concurrent log writes.
        # Ring buffer cap is 2000; the shallow copy cost is negligible.
        with self._log_lock:
            snapshot = list(reversed(self.log_entries))
        for entry in snapshot:
            if need_pid is not None and entry["plugin_id"] != need_pid:
                continue
            if need_lvl is not None and entry["level"] != need_lvl:
                continue
            collected.append(entry)
            if limit > 0 and len(collected) >= limit:
                break
        collected.reverse()
        return collected

    # ── File permissions ──

    # Check whether a plugin has access to the given path (paths normalized).
    # P1-6: resolve symlinks with realpath before prefix matching, preventing
    # a plugin from requesting permission with a legitimate prefix path then
    # reaching an out-of-whitelist target via a symlink.
    def check_file_permission(self, plugin_id: str, path: str) -> bool:
        perms = self.plugin_permissions.get(plugin_id, {})
        allowed = perms.get("allowed_paths", [])
        # Empty whitelist = allow all (test convenience)
        if not allowed:
            return True
        normalized = _safe_realpath(path)
        for allowed_path in allowed:
            norm_allowed = _safe_realpath(allowed_path)
            # Exact match or prefix match (with separator)
            if normalized == norm_allowed:
                return True
            if normalized.startswith(norm_allowed + os.sep):
                return True
        return False

    # Grant a plugin path access permissions.
    def grant_permission(self, plugin_id: str, allowed_paths: list[str]) -> None:
        self.plugin_permissions.setdefault(plugin_id, {})["allowed_paths"] = (
            allowed_paths
        )

    # ── API keys ──

    # Read an API key stored by Desk.
    # Compatible with Volcengine Claude Coding Plan auth:
    # provider="volcengine_claude" returns the Volcengine API key.
    def get_api_key(self, provider: str) -> str | None:
        if self.config_center is None:
            return None
        # Convention: config_center.get(f"api_keys.{provider}") returns ciphertext
        try:
            stored = self.config_center.get(f"api_keys.{provider}")
            return _decrypt_key(stored)
        except Exception:
            return None

    # Write an API key to the Desk config center (encrypted at rest, R6).
    def set_api_key(self, provider: str, key: str) -> None:
        if self.config_center is None:
            return
        try:
            self.config_center.set(f"api_keys.{provider}", _encrypt_key(key))
        except Exception as exc:
            logger.warning("desk_runtime: failed to write API key: %s", exc)

    # ── MLX inference ──

    # Call fusion-mlx local inference (fusion-mlx as the Claude vision/image
    # generation backend).
    async def mlx_chat(self, model: str, messages: list[dict], **kwargs: Any) -> Any:
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx client not injected")
        return await self.mlx_client.chat(model, messages, **kwargs)

    # Call fusion-mlx to generate text embeddings.
    async def mlx_embed(self, text: str, model: str = "BGE-M3") -> Any:
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx client not injected")
        return await self.mlx_client.embed(text, model=model)

    # Check whether fusion-mlx is healthy.
    async def mlx_health(self) -> bool:
        if self.mlx_client is None:
            return False
        return await self.mlx_client.health()

    # ── Node registry bridge ──

    # List node types registered in fusion-cowork.
    def list_nodes(self) -> list[dict[str, Any]]:
        if self.node_registry is None:
            return []
        return self.node_registry.list()

    # Resolve a node name (aliases supported) and return the node class.
    def resolve_node(self, name: str) -> Any | None:
        if self.node_registry is None:
            return None
        return self.node_registry.get(name)

    # ── Task scheduler bridge ──

    # List all scheduled tasks in fusion-cowork.
    def list_scheduled_tasks(self) -> list[Any]:
        if self.task_scheduler is None:
            return []
        return self.task_scheduler.list_tasks()

    # Return MCP gateway info (for Claude Desktop / Claude Code integration).
    def gateway_info(self) -> dict[str, Any]:
        return {
            "transport": "stdio",
            "port": self.mcp_gateway_port,
            "protocol_version": "2026-07-28",
        }
