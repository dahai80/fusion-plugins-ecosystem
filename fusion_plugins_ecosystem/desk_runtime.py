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
                "desk_runtime: strict mode with cryptography not installed, refusing to store API key in plaintext"
            ) from None
        logger.warning("desk_runtime: cryptography not installed, storing API key in plaintext")
        return plaintext
    try:
        return _KEY_ENC_PREFIX + Fernet(_derive_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")
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
        return Fernet(_derive_key()).decrypt(stored[len(_KEY_ENC_PREFIX) :].encode("ascii")).decode("utf-8")
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
    # fusion-cowork 任务调度器（fusion_cowork.TaskScheduler）
    task_scheduler: Any | None = None
    # fusion-cowork MLX 客户端（fusion_cowork.FusionMLXClient）
    mlx_client: Any | None = None
    # fusion-cowork 工作流引擎（fusion_cowork.WorkflowEngine）
    workflow_engine: Any | None = None
    # fusion-cowork 日志器
    desk_logger: logging.Logger | None = None
    # 配置中心句柄（dict-like，支持 get/set）
    config_center: Any | None = None
    # MCP 网关端口（由 cowork 分配）
    mcp_gateway_port: int | None = None
    # 已注册插件权限表 {plugin_id: {allowed_paths: [...], capabilities: [...]}}
    plugin_permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 显存占用台账 {plugin_id: mb}
    vram_allocations: dict[str, int] = field(default_factory=dict)
    # 总显存预算（MB），0 表示不限制
    vram_total_mb: int = 0
    # 已注册的插件 ID 集合
    registered_plugin_ids: set[str] = field(default_factory=set)
    # 日志环形缓冲区（供消费端查询历史日志）
    log_entries: deque = field(default_factory=lambda: deque(maxlen=_LOG_BUFFER_MAX))
    # 日志自增计数（生成日志条目 id）
    _log_counter: int = field(default=0)
    # vRAM 操作锁（线程安全）：acquire_vram/release_vram 是同步方法，
    # load/unload 同步路径会调用，asyncio.Lock 无法在同步代码里 await（P2-2）
    _vram_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 日志缓冲写锁：_log_counter 自增 + append 需原子，避免并发 id 重复/丢条目
    _log_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # R5：跨 handler 共享的 MCP 会话状态 + 速率限制时间戳。
    # 多个 MCPHandler 共用同一 DeskRuntime 时，限流/会话以 desk 为单一来源，
    # 避免 per-handler 各自计数导致 N 个 handler 放大 N 倍限流上限。
    _mcp_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    _mcp_call_timestamps: dict[str, Any] = field(default_factory=dict)
    _mcp_state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 可观测性指标注册表（企业级运维）：经此单例注入各模块，单一来源
    _metrics: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not FUSION_COWORK_AVAILABLE:
            logger.info("desk_runtime: fusion-cowork 未安装，DeskRuntime 以降级模式运行")

    @property
    def metrics(self) -> Any:
        """惰性初始化 MetricsRegistry 单例（避免循环导入）。"""
        if self._metrics is None:
            from fusion_plugins_ecosystem.metrics import MetricsRegistry

            self._metrics = MetricsRegistry()
        return self._metrics

    # ── 集群分布式状态桥接（企业级多节点，步骤 4）──
    # 集群未启用（FUSION_CLUSTER_ENABLED 非 truthy）或 fusion-cowork 缺失时，
    # 所有写操作 no-op、查询返回空——单机部署行为不变。

    def cluster_node_id(self) -> str | None:
        """本节点 id；集群未启用返回 None。"""
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.cluster_node_id()

    def cluster_heartbeat(
        self,
        host: str = "",
        port: int = 0,
        role: str = "worker",
        vram_total_mb: int = 0,
        vram_used_mb: int = 0,
        tags: list[str] | None = None,
    ) -> None:
        """向集群共享存储发送本节点心跳。集群未启用 no-op。"""
        from fusion_plugins_ecosystem import cluster_bridge

        cluster_bridge.cluster_heartbeat(
            host=host,
            port=port,
            role=role,
            vram_total_mb=vram_total_mb,
            vram_used_mb=vram_used_mb,
            tags=tags,
        )

    def cluster_evict_stale_nodes(self) -> list[str]:
        """failover：移除心跳超时的失效节点，清理其插件状态与 vRAM。"""
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.cluster_evict_stale_nodes()

    def is_plugin_enabled_anywhere(self, plugin_id: str) -> bool:
        """集群内任意节点是否已启用该插件。集群未启用返回 False。"""
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.is_plugin_enabled_anywhere(plugin_id)

    def plugin_state_across_cluster(self, plugin_id: str) -> list[dict[str, Any]]:
        """该插件在集群各节点的状态列表。集群未启用返回空列表。"""
        from fusion_plugins_ecosystem import cluster_bridge

        return cluster_bridge.plugin_state_across_cluster(plugin_id)

    # ── 显存调度 ──

    def acquire_vram(self, plugin_id: str, mb: int) -> bool:
        """向 Desk 申请显存（调整大小语义，非累加）。

        同一 plugin_id 再次调用会替换之前的分配量，
        而非在旧值上累加。

        check-then-set 在锁内原子完成，消除并发申请越过预算的 TOCTOU（P2-2）。
        """
        with self._vram_lock:
            if mb <= 0:
                self.vram_allocations.pop(plugin_id, None)
                return True
            current = self.vram_allocations.get(plugin_id, 0)
            delta = mb - current
            used = sum(self.vram_allocations.values())
            if self.vram_total_mb > 0 and used + delta > self.vram_total_mb:
                logger.warning(
                    "desk_runtime: 插件 %s 显存申请失败（%dMB 超预算 %dMB）",
                    plugin_id,
                    mb,
                    self.vram_total_mb,
                )
                return False
            self.vram_allocations[plugin_id] = mb
            logger.info("desk_runtime: 插件 %s 显存 %dMB→%dMB", plugin_id, current, mb)
            self.metrics.gauge("vram_used_mb", "已分配显存（MB）").set(sum(self.vram_allocations.values()))
            # 步骤 4：同步 vRAM 分配到集群共享存储（跨节点可见）
            from fusion_plugins_ecosystem import cluster_bridge

            cluster_bridge.record_vram(plugin_id, mb)
            return True

    def release_vram(self, plugin_id: str) -> None:
        """释放插件占用的显存。"""
        with self._vram_lock:
            freed = self.vram_allocations.pop(plugin_id, 0)
        if freed:
            logger.info("desk_runtime: 插件 %s 释放 %dMB 显存", plugin_id, freed)
        self.metrics.gauge("vram_used_mb", "已分配显存（MB）").set(sum(self.vram_allocations.values()))
        # 步骤 4：同步释放到集群共享存储
        from fusion_plugins_ecosystem import cluster_bridge

        cluster_bridge.release_vram(plugin_id)

    def vram_usage(self) -> dict[str, int]:
        """返回当前显存台账快照。"""
        return dict(self.vram_allocations)

    # ── 日志采集 ──

    def log(
        self,
        plugin_id: str,
        level: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        """统一日志采集入口（解决「子代理无日志」痛点）。

        所有插件日志经此汇集到 Desk 全链路日志面板，
        同时写入环形缓冲区供消费端查询历史日志。
        """
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
        # 写入环形缓冲区（供 plugins/logs.stream 查询）
        # counter 自增 + append 在锁内，避免并发 id 重复或 deque 调度丢条目
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

    def infra_log(
        self,
        component: str,
        level: str,
        message: str,
        **kwargs: Any,
    ) -> None:
        """基础设施事件统一采集（P2-9：双日志流割裂修复）。

        transport/jsonrpc/lifecycle/server 等基础层错误此前只走模块 logger
        (stderr)，运维 tail plugins/logs.stream 看不到——非法状态转移、
        handler 错误、鉴权拒绝、写回失败均不可查询。经此汇集到环形缓冲
        （plugin_id="_infra"），同时仍经 desk_logger 输出 stderr，形成
        单一可查询流。component 记入 kwargs 供按子系统过滤。
        """
        self.log("_infra", level, f"[{component}] {message}", component=component, **kwargs)

    def get_logs(
        self,
        plugin_id: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查询日志缓冲区（支持按插件/级别过滤，返回最近 limit 条）。

        供 plugins/logs.stream JSON-RPC 方法消费。

        逆序遍历 + 提前终止：只需最近 limit 条，不必全量 list() 拷贝再切片（P3-2）。
        """
        need_pid = plugin_id
        need_lvl = level.upper() if level is not None else None
        collected: list[dict[str, Any]] = []
        # R7：读路径快照 deque 后释放锁，避免长时间持锁阻塞并发写日志。
        # 环形缓冲上限 2000，浅拷贝开销可忽略。
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

    # ── 文件权限 ──

    def check_file_permission(self, plugin_id: str, path: str) -> bool:
        """检查插件是否具备对指定路径的访问权限（路径标准化）。

        P1-6：用 realpath 解析符号链接后再前缀匹配，防止插件以合法前缀路径
        申请权限后经符号链接指向白名单外目标越权访问。
        """
        perms = self.plugin_permissions.get(plugin_id, {})
        allowed = perms.get("allowed_paths", [])
        # 空白名单 = 允许全部（测试便利）
        if not allowed:
            return True
        normalized = _safe_realpath(path)
        for allowed_path in allowed:
            norm_allowed = _safe_realpath(allowed_path)
            # 精确匹配或前缀匹配（带分隔符）
            if normalized == norm_allowed:
                return True
            if normalized.startswith(norm_allowed + os.sep):
                return True
        return False

    def grant_permission(self, plugin_id: str, allowed_paths: list[str]) -> None:
        """授予插件路径访问权限。"""
        self.plugin_permissions.setdefault(plugin_id, {})["allowed_paths"] = allowed_paths

    # ── API 密钥 ──

    def get_api_key(self, provider: str) -> str | None:
        """读取 Desk 存储的 API 密钥。

        兼容火山方舟 Claude Coding Plan 套餐鉴权：
        provider="volcengine_claude" 返回火山方舟 API key。
        """
        if self.config_center is None:
            return None
        # 约定：config_center.get(f"api_keys.{provider}") 返回密文
        try:
            stored = self.config_center.get(f"api_keys.{provider}")
            return _decrypt_key(stored)
        except Exception:
            return None

    def set_api_key(self, provider: str, key: str) -> None:
        """写入 API 密钥到 Desk 配置中心（加密落盘，R6）。"""
        if self.config_center is None:
            return
        try:
            self.config_center.set(f"api_keys.{provider}", _encrypt_key(key))
        except Exception as exc:
            logger.warning("desk_runtime: 写入 API 密钥失败: %s", exc)

    # ── MLX 推理 ──

    async def mlx_chat(self, model: str, messages: list[dict], **kwargs: Any) -> Any:
        """调用 fusion-mlx 本地推理（fusion-mlx 作为 Claude 视觉/图像生成后端）。"""
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx 客户端未注入")
        return await self.mlx_client.chat(model, messages, **kwargs)

    async def mlx_embed(self, text: str, model: str = "BGE-M3") -> Any:
        """调用 fusion-mlx 生成文本向量。"""
        if self.mlx_client is None:
            raise RuntimeError("fusion-mlx 客户端未注入")
        return await self.mlx_client.embed(text, model=model)

    async def mlx_health(self) -> bool:
        """检查 fusion-mlx 是否健康。"""
        if self.mlx_client is None:
            return False
        return await self.mlx_client.health()

    # ── 节点注册表桥 ──

    def list_nodes(self) -> list[dict[str, Any]]:
        """列出 fusion-cowork 已注册的节点类型。"""
        if self.node_registry is None:
            return []
        return self.node_registry.list()

    def resolve_node(self, name: str) -> Any | None:
        """解析节点名称（支持别名）并返回节点类。"""
        if self.node_registry is None:
            return None
        return self.node_registry.get(name)

    # ── 任务调度器桥 ──

    def list_scheduled_tasks(self) -> list[Any]:
        """列出 fusion-cowork 的全部定时任务。"""
        if self.task_scheduler is None:
            return []
        return self.task_scheduler.list_tasks()

    def gateway_info(self) -> dict[str, Any]:
        """返回 MCP 网关信息（供 Claude Desktop / Claude Code 对接）。"""
        return {
            "transport": "stdio",
            "port": self.mcp_gateway_port,
            "protocol_version": "2026-07-28",
        }
