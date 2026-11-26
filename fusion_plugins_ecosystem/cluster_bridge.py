# Cluster distributed-state bridging (enterprise multi-node, step 4).
# Syncs plugin-ecosystem state changes into the fusion-cowork distributed
# state layer (fusion_cowork.distributed_state.DistributedStateStore):
# - plugin enabled/disabled consistency across nodes (plugin_states)
# - vRAM allocations visible across nodes (vram_allocations)
# - node heartbeats + stale-node failover (remove expired nodes and clean
#   up their state/VRAM)
# Degradation semantics: when the cluster is not enabled (FUSION_CLUSTER_
# ENABLED not truthy) or fusion-cowork is unavailable, all writes no-op and
# queries return the local view or empty — single-node deployment behavior
# is unchanged.

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# fusion-cowork availability gate (same as desk_runtime, optional dependency)
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
    # Return the distributed state store singleton; None when the cluster
    # is disabled or cowork is missing.
    if not _CLUSTER_AVAILABLE:
        return None
    if not is_cluster_enabled():
        return None
    try:
        return get_cluster_state_store()
    except Exception as exc:
        logger.warning("cluster_bridge: failed to get state store, degrading to no-op: %s", exc)
        return None


def record_plugin_state(plugin_id: str, installed: bool, enabled: bool) -> None:
    # Record plugin state into the cluster shared store (this node's view).
    # No-op when the cluster is disabled.
    store = cluster_store()
    if store is None:
        return
    try:
        store.record_plugin_state(plugin_id, installed=installed, enabled=enabled)
    except Exception as exc:
        logger.warning("cluster_bridge: record_plugin_state failed: %s", exc)


def record_vram(plugin_id: str, mb: int) -> None:
    # Record a vRAM allocation into the cluster shared store. mb<=0 counts
    # as a release.
    store = cluster_store()
    if store is None:
        return
    try:
        if mb > 0:
            store.record_vram_allocation(plugin_id, mb)
        else:
            store.release_vram_allocation(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: record_vram failed: %s", exc)


def release_vram(plugin_id: str) -> None:
    # Release plugin vRAM from the cluster shared store.
    store = cluster_store()
    if store is None:
        return
    try:
        store.release_vram_allocation(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: release_vram failed: %s", exc)


def is_plugin_enabled_anywhere(plugin_id: str) -> bool:
    # Whether any node in the cluster has the plugin enabled. Returns False
    # when the cluster is disabled (the local view lives in lifecycle).
    store = cluster_store()
    if store is None:
        return False
    try:
        return store.is_plugin_enabled_anywhere(plugin_id)
    except Exception as exc:
        logger.warning("cluster_bridge: is_plugin_enabled_anywhere failed: %s", exc)
        return False


def plugin_state_across_cluster(plugin_id: str) -> list[dict[str, Any]]:
    # Return this plugin's state list across cluster nodes. Empty list when
    # the cluster is disabled.
    store = cluster_store()
    if store is None:
        return []
    try:
        return [s.to_dict() for s in store.plugin_state_across_cluster(plugin_id)]
    except Exception as exc:
        logger.warning("cluster_bridge: plugin_state_across_cluster failed: %s", exc)
        return []


def cluster_heartbeat(
    host: str = "",
    port: int = 0,
    role: str = "worker",
    vram_total_mb: int = 0,
    vram_used_mb: int = 0,
    tags: list[str] | None = None,
) -> None:
    # Send this node's heartbeat to the cluster shared store. No-op when
    # the cluster is disabled.
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
        logger.warning("cluster_bridge: heartbeat failed: %s", exc)


def cluster_evict_stale_nodes() -> list[str]:
    # Failover: remove nodes whose heartbeat expired and clean up their
    # plugin state and vRAM.
    # Returns the list of removed node_ids. Empty list when the cluster is
    # disabled.
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
                logger.info("cluster_bridge: failover removed stale node %s", node.node_id)
        return evicted
    except Exception as exc:
        logger.warning("cluster_bridge: cluster_evict_stale_nodes failed: %s", exc)
        return []


def cluster_node_id() -> str | None:
    # Return this node's id; None when the cluster is disabled.
    store = cluster_store()
    if store is None:
        return None
    try:
        return store.node_id
    except Exception:
        return None
