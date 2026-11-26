"""Cluster distributed-state bridge tests (enterprise multi-node, step 4).

Verifies:
- bridge functions sync state to shared storage via a real DistributedStateStore
- cross-node visibility (node A enable -> node B is_plugin_enabled_anywhere=True)
- failover: stale node heartbeat timeout -> cluster_evict_stale_nodes removes it
  and cleans up its state/vRAM
- lifecycle.enable/disable sync cluster state via the bridge
- cluster disabled -> everything is a no-op (single-machine degradation)

Uses a real DistributedStateStore + shared temp state file directly, no mocks.
"""

from __future__ import annotations

import time

import pytest

# fusion-cowork is only available in the local monorepo venv; not installed in
# single-repo CI environments. When missing, skip the whole multi-node bridge
# suite (consistent with cluster_bridge's no-op degradation semantics).
pytest.importorskip("fusion_cowork")
from fusion_cowork.distributed_state import (  # noqa: E402
    DistributedStateStore,
    reset_cluster_state_store,
)


@pytest.fixture
def shared_state(tmp_path, monkeypatch):
    """Shared state file + cluster-enabled environment."""
    state_path = str(tmp_path / "cluster-state.json")
    monkeypatch.setenv("FUSION_CLUSTER_ENABLED", "1")
    monkeypatch.setenv("FUSION_CLUSTER_STATE_PATH", state_path)
    # Per-test isolated state file; reset the singleton cache
    reset_cluster_state_store()
    yield state_path
    reset_cluster_state_store()


def _store(node_id: str, state_path: str, heartbeat_timeout: float = 30.0):
    return DistributedStateStore(
        state_path=state_path, node_id=node_id, heartbeat_timeout=heartbeat_timeout
    )


# ── Cross-node visibility ──


def test_enable_on_node_a_visible_on_node_b(shared_state):
    """Node A records a plugin enable -> node B sees is_plugin_enabled_anywhere=True."""
    store_a = _store("node-a", shared_state)
    store_b = _store("node-b", shared_state)

    # Node A enables the plugin
    store_a.record_plugin_state("mlx_chat", installed=True, enabled=True)

    # From node B's perspective: enabled on any node in the cluster
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("mlx_chat") is True
    states = store_b.plugin_state_across_cluster("mlx_chat")
    assert len(states) == 1
    assert states[0].node_id == "node-a"
    assert states[0].enabled is True


def test_disable_on_node_a_reflects_on_node_b(shared_state):
    """Node A disables the plugin -> node B sees is_plugin_enabled_anywhere=False."""
    store_a = _store("node-a", shared_state)
    store_b = _store("node-b", shared_state)

    store_a.record_plugin_state("p1", installed=True, enabled=True)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("p1") is True

    store_a.record_plugin_state("p1", installed=True, enabled=False)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("p1") is False


def test_vram_allocation_visible_cluster_wide(shared_state):
    """Node A allocates vRAM -> the cluster total includes that allocation."""
    store_a = _store("node-a", shared_state)
    store_b = _store("node-b", shared_state)

    store_a.record_vram_allocation("vram_plugin", 2048)
    store_b.invalidate_cache()
    assert store_b.total_vram_allocated_mb() == 2048
    usage = store_b.cluster_vram_usage()
    assert usage.get("node-a") == 2048

    store_a.release_vram_allocation("vram_plugin")
    store_b.invalidate_cache()
    assert store_b.total_vram_allocated_mb() == 0


# ── failover ──


def test_evict_stale_node_cleans_state_and_vram(shared_state):
    """Stale node heartbeat timeout -> evict removes it and cleans up its plugin state and vRAM."""
    # Short timeout to make staleness easy to construct
    store_a = _store("node-a", shared_state, heartbeat_timeout=0.3)
    store_b = _store("node-b", shared_state, heartbeat_timeout=0.3)

    # Node A heartbeats + enables a plugin + allocates vRAM
    store_a.heartbeat(host="10.0.0.1", port=8765, vram_total_mb=8192, vram_used_mb=1024)
    store_a.record_plugin_state("failover_p", installed=True, enabled=True)
    store_a.record_vram_allocation("failover_p", 1024)

    # From node A's perspective the cluster contains itself; node B only
    # appears in the list after heartbeating
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("failover_p") is True

    # Wait for node A's heartbeat to time out
    time.sleep(0.4)

    # Node B initiates failover: remove the stale node-a
    evicted = []
    now = time.time()
    store_b.invalidate_cache()
    for node in store_b.list_all_nodes():
        if node.node_id != store_b.node_id and not node.is_alive(
            now, store_b.heartbeat_timeout
        ):
            store_b.remove_node(node.node_id)
            evicted.append(node.node_id)

    assert "node-a" in evicted
    # After cleanup, node-a's plugin state and vRAM are removed
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("failover_p") is False
    assert store_b.total_vram_allocated_mb() == 0


def test_heartbeat_keeps_node_alive(shared_state):
    """A node with active heartbeats is not evicted."""
    store_a = _store("node-a", shared_state, heartbeat_timeout=5.0)
    store_b = _store("node-b", shared_state, heartbeat_timeout=5.0)

    store_a.heartbeat(host="10.0.0.1", port=8765)
    store_b.heartbeat(host="10.0.0.2", port=8766)

    # From node B's perspective, live peers should include node-a
    store_b.invalidate_cache()
    peers = store_b.get_peer_nodes()
    assert any(p.node_id == "node-a" for p in peers)


# ── Bridge layer (cluster_bridge) syncing via the singleton ──


def test_cluster_bridge_noop_when_disabled(monkeypatch):
    """With the cluster disabled, bridge functions are all no-ops and queries return empty/False."""
    monkeypatch.delenv("FUSION_CLUSTER_ENABLED", raising=False)
    reset_cluster_state_store()

    from fusion_plugins_ecosystem import cluster_bridge

    # Not raising proves the no-op degradation
    cluster_bridge.record_plugin_state("p", installed=True, enabled=True)
    cluster_bridge.record_vram("p", 100)
    cluster_bridge.release_vram("p")
    cluster_bridge.cluster_heartbeat()
    assert cluster_bridge.is_plugin_enabled_anywhere("p") is False
    assert cluster_bridge.plugin_state_across_cluster("p") == []
    assert cluster_bridge.cluster_evict_stale_nodes() == []
    assert cluster_bridge.cluster_node_id() is None


def test_cluster_bridge_syncs_via_singleton(shared_state, monkeypatch):
    """Bridge syncs via the singleton store: node-a bridge record -> visible to a same-node store query."""
    monkeypatch.setenv("FUSION_CLUSTER_NODE_ID", "node-a")
    reset_cluster_state_store()

    from fusion_plugins_ecosystem import cluster_bridge

    cluster_bridge.record_plugin_state("bridge_p", installed=True, enabled=True)
    assert cluster_bridge.is_plugin_enabled_anywhere("bridge_p") is True

    cluster_bridge.record_vram("bridge_p", 512)
    assert cluster_bridge.cluster_node_id() == "node-a"


# ── lifecycle syncing cluster state via the bridge ──


async def test_lifecycle_enable_syncs_cluster_state(shared_state, monkeypatch):
    """lifecycle.enable -> cluster shared storage records the plugin as enabled (cross-node visible)."""
    monkeypatch.setenv("FUSION_CLUSTER_NODE_ID", "node-lc")
    reset_cluster_state_store()

    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
    from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
        PluginRegistry,
    )

    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    registry.register(
        PluginManifest(
            id="lc_plugin",
            name="LC Plugin",
            version="0.1.0",
            category=PluginCategory.CUSTOM,
            description="lifecycle cluster sync",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=lambda d, p: {"ok": True},
            timeout_seconds=30,
        )
    )
    lifecycle = PluginLifecycle(registry)
    lifecycle.load("lc_plugin")
    await lifecycle.enable("lc_plugin")

    # From node B's perspective (new store, same state file)
    store_b = _store("node-b", shared_state)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("lc_plugin") is True

    await lifecycle.disable("lc_plugin")
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("lc_plugin") is False


# ── DeskRuntime bridge methods ──


def test_desk_cluster_query_methods(shared_state, monkeypatch):
    """DeskRuntime's exposed cluster query methods take effect via the bridge."""
    monkeypatch.setenv("FUSION_CLUSTER_NODE_ID", "node-desk")
    reset_cluster_state_store()

    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    desk = DeskRuntime()
    assert desk.cluster_node_id() == "node-desk"

    # Direct bridge write outside the lifecycle path
    from fusion_plugins_ecosystem import cluster_bridge

    cluster_bridge.record_plugin_state("desk_p", installed=True, enabled=True)
    assert desk.is_plugin_enabled_anywhere("desk_p") is True
    states = desk.plugin_state_across_cluster("desk_p")
    assert len(states) == 1
    assert states[0]["enabled"] is True
