"""稳定注册表读契约测试（issue #44 PC-4：单注册表三表面）。

契约 registry/1.0：fusion-cowork#228 能力市场与 fusion-agent-studio#529
构建器建议消费 `/api/registry/capabilities` — 只读授权态，授权动作跳 hub；
消费方不得分叉缓存（单一事实源）。
"""

import sys
from pathlib import Path

import pytest

from fusion_plugins_ecosystem.extensions_console import ExtensionsConsole
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import PluginRegistry

sys.path.insert(0, str(Path(__file__).parent))
from test_install_flow import BUNDLE, _manifest, _write_index  # noqa: E402
from test_extensions_console import FakeDesk  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    monkeypatch.setenv("FUSION_PLUGIN_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("FUSION_PLUGINS_CONSOLE_API_KEY", raising=False)
    from fusion_plugins_ecosystem import shell_registry

    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key")
    monkeypatch.setenv(
        "FUSION_PLUGIN_MARKETPLACE_INDEX", str(tmp_path / "market_index.json")
    )


@pytest.fixture()
def console(tmp_path):
    desk = FakeDesk()
    registry = PluginRegistry(desk)
    lifecycle = PluginLifecycle(registry)
    console = ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)
    console.registry.register(_manifest("excel-skill", ["claude_skill"]))
    console.registry.register(_manifest("bank-recon-agent", ["subagent"]))
    _write_index(tmp_path, [BUNDLE])
    return console


def _by_id(console, cap_id):
    listing = console.registry_capabilities()
    assert listing["contract"] == "registry/1.0"
    return next(c for c in listing["capabilities"] if c["id"] == cap_id)


class TestContract:
    async def test_kinds_and_install_state(self, console):
        await console.install_bundle("finance-kit")
        skill = _by_id(console, "node.excel-skill")
        assert skill["kind"] == "skill" and skill["enabled"] is True
        agent = _by_id(console, "node.bank-recon-agent")
        assert agent["kind"] == "subagent"
        # 停用即从消费方消失（enabled=false — 消费方按 enabled 过滤）
        await console.tick_bundle_item("finance-kit", "excel-skill", False)
        assert _by_id(console, "node.excel-skill")["enabled"] is False

    async def test_connector_pack_bridge_listed(self, console):
        # 连接器包桥接（无 rest 文档）按桥接清单入列，授权面全量
        console.registry.register(_manifest("connector__packdemo__search", []))
        cap = _by_id(console, "connector.packdemo.search")
        assert cap["kind"] == "connector"
        assert cap["auth"]["state"] == "granted"
        assert cap["auth"]["all_scopes_granted"] is True

    async def test_resync_rebuilds_bundle_items(self, console):
        # issue #44：重启后 enabled bundle 项重建 — 消费方拉取不"消失"
        from fusion_plugins_ecosystem.connector_packs import NOTION_PACK

        pack_ids = [f"connector__notion__{t.name}" for t in NOTION_PACK.tools]
        await console.install_bundle("finance-kit")
        # 模拟重启：新 console 共享磁盘状态，registry 清空
        fresh = ExtensionsConsole(
            desk=console.desk,
            registry=PluginRegistry(console.desk),
            lifecycle=None,
        )
        assert not any(
            m.id.startswith("connector__bank-api__") for m in fresh.registry.list()
        )
        # 重启后清单来源（内置/自定义声明）重新注册 — 测试里手工补
        fresh.registry.register(_manifest("excel-skill", ["claude_skill"]))
        out = await fresh.resync()
        assert "excel-skill" in out["relisted"]
        assert fresh._plugin_enabled("excel-skill")
        # rest 连接器（pending_auth 未授权）按 resync 既有路径桥接
        assert any(
            m.id.startswith("connector__bank-api__") for m in fresh.registry.list()
        )
        # 连接器包：enabled 项重装（用 finance-kit 无 pack，补一个 pack bundle 验证路径）
        state = fresh._bundles_state()
        state["pack-bundle"] = {
            "id": "pack-bundle",
            "items": [{"type": "connector_pack", "ref": "notion", "state": "enabled"}],
        }
        fresh._save_bundles_state(state)
        await fresh.resync()
        ids = {m.id for m in fresh.registry.list()}
        assert any(pid in ids for pid in pack_ids)
        # 重建后工具处于 ENABLED（安装路径与 tick-on 一致）
        assert any(fresh._plugin_enabled(pid) for pid in pack_ids)

    async def test_pack_install_enables_tools(self, console):
        # 安装路径即启用：bundle 项 state=enabled 与工具生命周期态一致
        out = await console._install_bundle_item("connector_pack", "notion", {})
        assert out["state"] == "enabled"
        assert console._plugin_enabled(out["plugin_ids"][0])

    async def test_uninstall_leaves_no_pack_orphans(self, console):
        # issue #44 验收：hub 卸载后注册表无残留（消费方拉取干净）
        state = console._bundles_state()
        state["pack-bundle"] = {
            "id": "pack-bundle",
            "items": [{"type": "connector_pack", "ref": "notion", "state": "enabled"}],
        }
        console._save_bundles_state(state)
        await console._install_bundle_item("connector_pack", "notion", {})
        assert console._pack_plugin_ids("notion")
        out = await console.uninstall_bundle("pack-bundle")
        assert out["removed"] is True and "notion" in out["items_removed"]
        assert console._pack_plugin_ids("notion") == []

    async def test_connector_auth_state_single_source(self, console):
        await console.install_bundle("finance-kit")
        # pending_auth：通道可见但未授权暴露
        assert (
            _by_id(console, "connector.bank-api.bank.flow.fetch")["auth"]["state"]
            == "pending"
        )
        await console.oauth_complete(
            "bank-api", ["bank.flow.fetch", "bank.account.fetch"], {"api_key": "k"}
        )
        cap = _by_id(console, "connector.bank-api.bank.flow.fetch")
        assert cap["kind"] == "connector"
        assert cap["auth"]["state"] == "granted"
        assert cap["auth"]["granted"] is True
        assert cap["auth"]["all_scopes_granted"] is False
        # hub 撤销 → 契约面下次拉取即反映（无同步任务）
        await console.revoke_scopes("bank-api", ["bank.flow.fetch"])
        cap = _by_id(console, "connector.bank-api.bank.flow.fetch")
        assert cap["auth"]["state"] == "revoked" and cap["auth"]["granted"] is False
        assert cap["auth"]["hub_url"] == "/shell/fusion-plugins-ecosystem/"

    async def test_detail_endpoint_data(self, console):
        from test_install_flow import _FakeNode, _FakeNodeRegistry

        await console.install_bundle("finance-kit")
        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        console.desk.node_registry = _FakeNodeRegistry(
            [_FakeNode("月度对账", "使用 connector.bank-api.bank.flow.fetch")]
        )
        out = console.registry_capability("connector.bank-api.bank.flow.fetch")
        assert out["kind"] == "connector"
        assert out["per_use_scopes"] == ["bank.order.push"]
        assert out["in_use_by"][0]["node"] == "月度对账"
        assert "error" in console.registry_capability("node.missing")

    async def test_http_registry_endpoints(self, console):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fusion_plugins_ecosystem.console_app import create_app

        client = TestClient(
            create_app(
                desk=console.desk,
                registry=console.registry,
                lifecycle=console.lifecycle,
            )
        )
        await console.install_bundle("finance-kit")
        r = client.get("/api/registry/capabilities")
        assert r.status_code == 200
        body = r.json()
        assert body["contract"] == "registry/1.0"
        assert any(c["id"] == "node.excel-skill" for c in body["capabilities"])
        r = client.get("/api/registry/capabilities/node.excel-skill")
        assert r.status_code == 200 and r.json()["kind"] == "skill"
        r = client.get("/api/registry/capabilities/node.missing")
        assert r.status_code == 404
