"""集群分布式状态桥接测试（企业级多节点，步骤 4）。

验证：
- 桥接函数经真实 DistributedStateStore 同步状态到共享存储
- 跨节点可见性（节点 A enable → 节点 B is_plugin_enabled_anywhere=True）
- failover：失效节点心跳超时 → cluster_evict_stale_nodes 移除并清理其状态/vRAM
- lifecycle.enable/disable 经桥接同步集群状态
- 集群未启用时全 no-op（单机降级）

直接用真实 DistributedStateStore + 共享临时状态文件，不经 mock。
"""

from __future__ import annotations

import time

import pytest

# fusion-cowork 仅在本地 monorepo venv 可用；CI 单仓环境未安装。
# 缺失时整个多节点桥接测试跳过（与 cluster_bridge 的 no-op 降级语义一致）。
pytest.importorskip("fusion_cowork")
from fusion_cowork.distributed_state import (  # noqa: E402
    DistributedStateStore,
    reset_cluster_state_store,
)


@pytest.fixture
def shared_state(tmp_path, monkeypatch):
    """共享状态文件 + 集群启用环境。"""
    state_path = str(tmp_path / "cluster-state.json")
    monkeypatch.setenv("FUSION_CLUSTER_ENABLED", "1")
    monkeypatch.setenv("FUSION_CLUSTER_STATE_PATH", state_path)
    # 每个测试用独立状态文件，重置单例缓存
    reset_cluster_state_store()
    yield state_path
    reset_cluster_state_store()


def _store(node_id: str, state_path: str, heartbeat_timeout: float = 30.0):
    return DistributedStateStore(
        state_path=state_path, node_id=node_id, heartbeat_timeout=heartbeat_timeout
    )


# ── 跨节点可见性 ──


def test_enable_on_node_a_visible_on_node_b(shared_state):
    """节点 A 记录插件启用 → 节点 B 查询 is_plugin_enabled_anywhere=True。"""
    store_a = _store("node-a", shared_state)
    store_b = _store("node-b", shared_state)

    # 节点 A 启用插件
    store_a.record_plugin_state("mlx_chat", installed=True, enabled=True)

    # 节点 B 视角：集群内任意节点已启用
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("mlx_chat") is True
    states = store_b.plugin_state_across_cluster("mlx_chat")
    assert len(states) == 1
    assert states[0].node_id == "node-a"
    assert states[0].enabled is True


def test_disable_on_node_a_reflects_on_node_b(shared_state):
    """节点 A 禁用插件 → 节点 B is_plugin_enabled_anywhere=False。"""
    store_a = _store("node-a", shared_state)
    store_b = _store("node-b", shared_state)

    store_a.record_plugin_state("p1", installed=True, enabled=True)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("p1") is True

    store_a.record_plugin_state("p1", installed=True, enabled=False)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("p1") is False


def test_vram_allocation_visible_cluster_wide(shared_state):
    """节点 A 分配 vRAM → 集群总量统计含该分配。"""
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
    """失效节点心跳超时 → evict 移除并清理其插件状态与 vRAM。"""
    # 短超时便于构造 stale
    store_a = _store("node-a", shared_state, heartbeat_timeout=0.3)
    store_b = _store("node-b", shared_state, heartbeat_timeout=0.3)

    # 节点 A 心跳 + 启用插件 + 分配 vRAM
    store_a.heartbeat(host="10.0.0.1", port=8765, vram_total_mb=8192, vram_used_mb=1024)
    store_a.record_plugin_state("failover_p", installed=True, enabled=True)
    store_a.record_vram_allocation("failover_p", 1024)

    # 节点 A 视角下集群含自己 + 节点 B 需心跳才在列表
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("failover_p") is True

    # 等待节点 A 心跳超时
    time.sleep(0.4)

    # 节点 B 发起 failover：移除失效的 node-a
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
    # 清理后 node-a 的插件状态、vRAM 已移除
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("failover_p") is False
    assert store_b.total_vram_allocated_mb() == 0


def test_heartbeat_keeps_node_alive(shared_state):
    """活跃心跳节点不被 evict。"""
    store_a = _store("node-a", shared_state, heartbeat_timeout=5.0)
    store_b = _store("node-b", shared_state, heartbeat_timeout=5.0)

    store_a.heartbeat(host="10.0.0.1", port=8765)
    store_b.heartbeat(host="10.0.0.2", port=8766)

    # 节点 B 视角的活跃 peer 应含 node-a
    store_b.invalidate_cache()
    peers = store_b.get_peer_nodes()
    assert any(p.node_id == "node-a" for p in peers)


# ── 桥接层（cluster_bridge）经单例同步 ──


def test_cluster_bridge_noop_when_disabled(monkeypatch):
    """集群未启用时桥接函数全 no-op、查询返回空/False。"""
    monkeypatch.delenv("FUSION_CLUSTER_ENABLED", raising=False)
    reset_cluster_state_store()

    from fusion_plugins_ecosystem import cluster_bridge

    # 不抛异常即证明 no-op 降级
    cluster_bridge.record_plugin_state("p", installed=True, enabled=True)
    cluster_bridge.record_vram("p", 100)
    cluster_bridge.release_vram("p")
    cluster_bridge.cluster_heartbeat()
    assert cluster_bridge.is_plugin_enabled_anywhere("p") is False
    assert cluster_bridge.plugin_state_across_cluster("p") == []
    assert cluster_bridge.cluster_evict_stale_nodes() == []
    assert cluster_bridge.cluster_node_id() is None


def test_cluster_bridge_syncs_via_singleton(shared_state, monkeypatch):
    """桥接经单例 store 同步：node-a 桥接记录 → 同节点 store 查询可见。"""
    monkeypatch.setenv("FUSION_CLUSTER_NODE_ID", "node-a")
    reset_cluster_state_store()

    from fusion_plugins_ecosystem import cluster_bridge

    cluster_bridge.record_plugin_state("bridge_p", installed=True, enabled=True)
    assert cluster_bridge.is_plugin_enabled_anywhere("bridge_p") is True

    cluster_bridge.record_vram("bridge_p", 512)
    assert cluster_bridge.cluster_node_id() == "node-a"


# ── lifecycle 经桥接同步集群状态 ──


async def test_lifecycle_enable_syncs_cluster_state(shared_state, monkeypatch):
    """lifecycle.enable → 集群共享存储记录该插件启用（跨节点可见）。"""
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

    # 节点 B 视角（新 store，同状态文件）
    store_b = _store("node-b", shared_state)
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("lc_plugin") is True

    await lifecycle.disable("lc_plugin")
    store_b.invalidate_cache()
    assert store_b.is_plugin_enabled_anywhere("lc_plugin") is False


# ── DeskRuntime 桥接方法 ──


def test_desk_cluster_query_methods(shared_state, monkeypatch):
    """DeskRuntime 暴露的集群查询方法经桥接生效。"""
    monkeypatch.setenv("FUSION_CLUSTER_NODE_ID", "node-desk")
    reset_cluster_state_store()

    from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

    desk = DeskRuntime()
    assert desk.cluster_node_id() == "node-desk"

    # 经 lifecycle 之外的直接桥接写入
    from fusion_plugins_ecosystem import cluster_bridge

    cluster_bridge.record_plugin_state("desk_p", installed=True, enabled=True)
    assert desk.is_plugin_enabled_anywhere("desk_p") is True
    states = desk.plugin_state_across_cluster("desk_p")
    assert len(states) == 1
    assert states[0]["enabled"] is True
