"""集群分布式状态桥接（企业级多节点，步骤 4）。

将插件生态的状态变更同步到 fusion-cowork 分布式状态层
（fusion_cowork.distributed_state.DistributedStateStore），实现：
- 插件 enabled/disabled 跨节点一致（plugin_states）
- vRAM 分配跨节点可见（vram_allocations）
- 节点心跳 + 失效节点 failover（移除过期节点 + 清理其状态/显存）

降级语义：集群未启用（FUSION_CLUSTER_ENABLED 非 truthy）或 fusion-cowork
不可用时，所有写操作 no-op，查询返回本机视图或空——单机部署行为不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# fusion-cowork 可用性门控（与 desk_runtime 一致，可选依赖）
_CLUSTER_AVAILABLE = False
try:
    from fusion_cowork.distributed_state import (
        get_cluster_state_store,
        is_cluster_enabled,
    )

    _CLUSTER_AVAILABLE = True
except ImportError:
    pass


def cluster_store() -> Any | None:
    """返回分布式状态存储单例；集群未启用或 cowork 缺失返回 None。"""
    if not _CLUSTER_AVAILABLE:
        return None
    if not is_cluster_enabled():
        return None
    try:
        return get_cluster_state_store()
    except Exception as exc:
        logger.warning("cluster_bridge: 获取状态存储失败，降级 no-op: %s", exc)
        return None


def record_plugin_state(plugin_id: str, installed: bool, enabled: bool) -> None:
    """记录插件状态到集群共享存储（本节点视角）。集群未启用时 no-op。"""
    store = cluster_store()
    if store is None:
        return
    try:
        store.record_plugin_state(plugin_id, installed=installed, enabled=enabled)
    except Exception as exc:
        logger.warning("cluster_bridge: record_plugin_state 失败: %s", exc)


def record_vram(plugin_id: str, mb: int) -> None:
    """记录 vRAM 分配到集群共享存储。mb<=0 视为释放。"""
    store = cluster_store()
    if store is None:
        return
    try:
        if mb > 0:
            store.record_vram_allocation(plugin_id, mb)
        else:
            store.release_vram_allocation(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: record_vram 失败: %s", exc)


def release_vram(plugin_id: str) -> None:
    """从集群共享存储释放插件 vRAM。"""
    store = cluster_store()
    if store is None:
        return
    try:
        store.release_vram_allocation(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: release_vram 失败: %s", exc)


def is_plugin_enabled_anywhere(plugin_id: str) -> bool:
    """集群内任意节点是否已启用该插件。集群未启用返回 False（本机视图由 lifecycle 持有）。"""
    store = cluster_store()
    if store is None:
        return False
    try:
        return store.is_plugin_enabled_anywhere(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: is_plugin_enabled_anywhere 失败: %s", exc)
        return False


def plugin_state_across_cluster(plugin_id: str) -> list[dict[str, Any]]:
    """返回该插件在集群各节点的状态列表。集群未启用返回空列表。"""
    store = cluster_store()
    if store is None:
        return []
    try:
        return [s.to_dict() for s in store.plugin_state_across_cluster(plugin_id)]
    except Exception as exc:
        logger.warning("cluster_bridge: plugin_state_across_cluster 失败: %s", exc)
        return []


def cluster_heartbeat(
    host: str = "",
    port: int = 0,
    role: str = "worker",
    vram_total_mb: int = 0,
    vram_used_mb: int = 0,
    tags: list[str] | None = None,
) -> None:
    """向集群共享存储发送本节点心跳。集群未启用 no-op。"""
    store = cluster_store()
    if store is None:
        return
    try:
        store.heartbeat(
            host=host,
            port=port,
            role=role,
            vram_total_mb=vram_total_mb,
            vram_used_mb=vram_used_mb,
            tags=tags,
        )
    except Exception as exc:
        logger.warning("cluster_bridge: heartbeat 失败: %s", exc)


def cluster_evict_stale_nodes() -> list[str]:
    """failover：移除心跳超时的失效节点，清理其插件状态与 vRAM。

    返回被移除的 node_id 列表。集群未启用返回空列表。
    """
    store = cluster_store()
    if store is None:
        return []
    try:
        now = time.time()
        evicted: list[str] = []
        for node in store.list_all_nodes():
            if node.node_id == store.node_id:
                continue
            if not node.is_alive(now, store.heartbeat_timeout):
                store.remove_node(node.node_id)
                evicted.append(node.node_id)
                logger.info("cluster_bridge: failover 移除失效节点 %s", node.node_id)
        return evicted
    except Exception as exc:
        logger.warning("cluster_bridge: cluster_evict_stale_nodes 失败: %s", exc)
        return []


def cluster_node_id() -> str | None:
    """返回本节点 id；集群未启用返回 None。"""
    store = cluster_store()
    if store is None:
        return None
    try:
        return store.node_id
    except Exception:
        return None
