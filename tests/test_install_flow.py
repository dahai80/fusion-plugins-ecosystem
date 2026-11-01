"""安装/授权/更新/卸载流测试（issue #42 PC-2）。

覆盖: bundle 一键安装（逐项状态）、逐项停用、连接器 OAuth（scope 授权 +
敏感 scope per_use 策略）、changelog 先行更新 + opt-in、卸载影响面
（被使用于反向查找）、HTTP 端点、市场 bundle kind 校验。
"""

import json
import sys
from pathlib import Path

import pytest

from fusion_plugins_ecosystem.extensions_console import ExtensionsConsole
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.rest_connector import get_connector

sys.path.insert(0, str(Path(__file__).parent))
from test_extensions_console import FakeDesk  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))
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
    return ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)


def _manifest(pid, caps, name=None):
    return PluginManifest(
        id=pid,
        name=name or pid,
        version="1.0.0",
        category=PluginCategory.CUSTOM,
        description="",
        capabilities=tuple(PluginCapability(c) for c in caps),
        entry_point="json:dump",
    )


def _write_index(tmp_path, packages):
    (tmp_path / "market_index.json").write_text(
        json.dumps({"packages": packages}, ensure_ascii=False), encoding="utf-8"
    )


BUNDLE = {
    "id": "finance-kit",
    "kind": "bundle",
    "name": "财务套件",
    "version": "1.0.0",
    "description": "首个版本",
    "items": [
        {"type": "skill", "ref": "excel-skill"},
        {"type": "subagent", "ref": "bank-recon-agent"},
        {
            "type": "connector",
            "ref": "bank-api",
            "doc": {
                "kind": "rest",
                "base_url": "https://bank.example.com/api",
                "credential_ref": "bank-api",
                "auth": {"type": "bearer", "credential_fields": ["api_key"]},
                "operations": {
                    "bank.flow.fetch": {
                        "method": "GET",
                        "path": "/flow",
                        "response": {"rows": "$.rows"},
                    },
                    "bank.account.fetch": {
                        "method": "GET",
                        "path": "/account",
                    },
                    "bank.order.push": {"method": "POST", "path": "/orders"},
                },
            },
        },
    ],
}


@pytest.fixture()
def populated(console, tmp_path):
    console.registry.register(_manifest("excel-skill", ["claude_skill"]))
    console.registry.register(_manifest("bank-recon-agent", ["subagent"]))
    _write_index(tmp_path, [BUNDLE])
    return console


class TestInstallBundle:
    async def test_install_states(self, populated):
        out = await populated.install_bundle("finance-kit")
        assert out["installed"] is True
        states = {i["ref"]: i["state"] for i in out["items"]}
        # skills 启用、子代理注册启用、连接器落盘置 pending_auth
        assert states["excel-skill"] == "enabled"
        assert states["bank-recon-agent"] == "enabled"
        assert states["bank-api"] == "pending_auth"
        assert populated._plugin_enabled("excel-skill") is True
        assert get_connector("bank-api") is not None

    async def test_install_missing_skill_reports_error(self, console, tmp_path):
        _write_index(tmp_path, [BUNDLE])
        out = await console.install_bundle("finance-kit")
        assert out["errors"], "missing skill must surface per-item error"

    async def test_install_unknown_entry_404(self, populated):
        with pytest.raises(KeyError):
            await populated.install_bundle("no-such-bundle")

    def test_bundle_listed(self, populated):
        populated._market_entry("finance-kit")  # 索引可见
        assert populated.list_bundles()["bundles"] == []

    async def test_single_pack_entry_synthesizes_bundle(self, console, tmp_path):
        # 内置 connector_pack 条目 → 单条目 bundle（推荐位一键安装路径）
        out = await console.install_bundle("feishu")
        assert out["installed"] is True
        item = out["items"][0]
        assert item["type"] == "connector_pack"
        assert item["state"] == "enabled"
        assert item["plugin_ids"]


class TestPerItemTick:
    async def test_disable_and_reenable(self, populated):
        await populated.install_bundle("finance-kit")
        out = await populated.tick_bundle_item("finance-kit", "excel-skill", False)
        assert out["ticked"] is True and out["state"] == "disabled"
        assert populated._plugin_enabled("excel-skill") is False
        out = await populated.tick_bundle_item("finance-kit", "excel-skill", True)
        assert out["ticked"] is True and out["state"] == "enabled"

    async def test_pending_auth_cannot_enable(self, populated):
        await populated.install_bundle("finance-kit")
        out = await populated.tick_bundle_item("finance-kit", "bank-api", True)
        assert out["ticked"] is False
        assert "待授权" in out["error"]

    async def test_unknown_bundle_or_ref(self, populated):
        await populated.install_bundle("finance-kit")
        assert (await populated.tick_bundle_item("nope", "x", True))["ticked"] is False
        out = await populated.tick_bundle_item("finance-kit", "ghost", True)
        assert out["ticked"] is False


class TestOAuth:
    async def test_oauth_flow(self, populated):
        await populated.install_bundle("finance-kit")
        scopes = populated.oauth_scopes("bank-api")
        # 读 scope 非敏感 + 写 scope 敏感
        by_id = {s["id"]: s for s in scopes["scopes"]}
        assert by_id["bank.flow.fetch"]["sensitive"] is False
        assert by_id["bank.order.push"]["sensitive"] is True
        assert scopes["credential_fields"] == ["api_key"]
        # 只授权一个读 scope
        out = await populated.oauth_complete(
            "bank-api", ["bank.flow.fetch"], {"api_key": "k1"}
        )
        assert out["completed"] is True
        assert out["granted_scopes"] == ["bank.flow.fetch"]
        assert out["sensitive_scopes_per_use"] == ["bank.order.push"]
        # 未授权读通道不桥接，授权通道暴露
        assert populated.registry.get("connector__bank-api__bank.flow.fetch")
        assert populated.registry.get("connector__bank-api__bank.account.fetch") is None
        assert populated.registry.get("connector__bank-api__bank.order.push") is None
        # 敏感 scope 策略可查询（cowork 审批对接点）
        policy = populated.scope_policy("bank-api")
        assert policy["per_use_scopes"] == ["bank.order.push"]

    async def test_oauth_unknown_scope_rejected(self, populated):
        await populated.install_bundle("finance-kit")
        out = await populated.oauth_complete("bank-api", ["nope"], {"api_key": "k"})
        assert out["completed"] is False
        assert "未知读 scope" in out["error"]

    async def test_oauth_requires_credentials(self, populated):
        await populated.install_bundle("finance-kit")
        out = await populated.oauth_complete("bank-api", [], {})
        assert out["completed"] is False

    async def test_oauth_state_flips_bundle_item(self, populated):
        await populated.install_bundle("finance-kit")
        await populated.oauth_complete("bank-api", [], {"api_key": "k"})
        bundles = populated.list_bundles()["bundles"]
        item = next(
            i for b in bundles for i in b["items"] if i.get("ref") == "bank-api"
        )
        assert item["state"] == "enabled"


class TestUpdate:
    async def test_update_preview_changelog_first(self, populated, tmp_path):
        await populated.install_bundle("finance-kit")
        v2 = json.loads(json.dumps(BUNDLE))
        v2["version"] = "1.1.0"
        v2["description"] = "新增发票 Skill，修复流水读取"
        v2["items"].append({"type": "skill", "ref": "invoice-skill"})
        _write_index(tmp_path, [v2])
        populated.registry.register(_manifest("invoice-skill", ["claude_skill"]))
        pv = populated.bundle_update_preview("finance-kit")
        assert pv["update_available"] is True
        assert pv["changelog"] == "新增发票 Skill，修复流水读取"
        assert pv["new_items"] == [{"type": "skill", "ref": "invoice-skill"}]
        # 未应用前 invoice-skill 未启用
        assert populated._plugin_enabled("invoice-skill") is False

    async def test_update_apply_opt_in_only(self, populated, tmp_path):
        await populated.install_bundle("finance-kit")
        v2 = json.loads(json.dumps(BUNDLE))
        v2["version"] = "1.1.0"
        v2["items"].append({"type": "skill", "ref": "invoice-skill"})
        v2["items"].append({"type": "subagent", "ref": "tax-agent"})
        _write_index(tmp_path, [v2])
        populated.registry.register(_manifest("invoice-skill", ["claude_skill"]))
        populated.registry.register(_manifest("tax-agent", ["subagent"]))
        out = await populated.bundle_update_apply("finance-kit", ["invoice-skill"])
        assert out["updated"] is True
        assert out["version"] == "1.1.0"
        # opt-in 项启用，未勾选项不自动开
        assert populated._plugin_enabled("invoice-skill") is True
        assert populated._plugin_enabled("tax-agent") is False
        # 版本已前移，二次 preview 无更新
        assert (
            populated.bundle_update_preview("finance-kit")["update_available"] is False
        )

    async def test_update_none_available(self, populated):
        await populated.install_bundle("finance-kit")
        assert (
            populated.bundle_update_preview("finance-kit")["update_available"] is False
        )
        out = await populated.bundle_update_apply("finance-kit", [])
        assert out["updated"] is False


class _FakeNode:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _FakeNodeRegistry:
    def __init__(self, nodes):
        self._nodes = nodes

    def list(self):
        return self._nodes


class TestUninstall:
    async def test_blast_radius_reverse_lookup(self, populated):
        await populated.install_bundle("finance-kit")
        # excel-skill 被一个 cowork 节点引用
        populated.desk.node_registry = _FakeNodeRegistry(
            [_FakeNode("月度对账", "使用 node.excel-skill 读取表格")]
        )
        pv = populated.uninstall_preview("finance-kit")
        assert pv["safe_to_remove"] is False
        assert pv["in_use_by"] == [
            {"node": "月度对账", "refs": ["excel-skill", "node.excel-skill"]}
        ]
        assert "node.excel-skill" in pv["capabilities_affected"]

    async def test_uninstall_removes_items(self, populated):
        await populated.install_bundle("finance-kit")
        out = await populated.uninstall_bundle("finance-kit")
        assert out["removed"] is True
        assert "bank-api" in out["items_removed"]
        assert get_connector("bank-api") is None
        assert populated._plugin_enabled("excel-skill") is False
        assert populated.list_bundles()["bundles"] == []

    async def test_uninstall_unknown(self, populated):
        out = await populated.uninstall_bundle("ghost")
        assert out["removed"] is False


class TestMarketplaceBundleKind:
    def test_bundle_entry_validation(self, tmp_path):
        from fusion_plugins_ecosystem.marketplace import Marketplace

        idx = tmp_path / "idx.json"
        idx.write_text(
            json.dumps(
                {
                    "packages": [
                        {
                            "id": "ok",
                            "kind": "bundle",
                            "items": [{"type": "skill", "ref": "s"}],
                        },
                        {"id": "bad-items", "kind": "bundle", "items": []},
                        {
                            "id": "bad-type",
                            "kind": "bundle",
                            "items": [{"type": "nope", "ref": "x"}],
                        },
                        {"id": "no-items", "kind": "bundle"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        mp = Marketplace(connector_registry=None, marketplace_index=str(idx))
        entries, warnings = mp.load_index()
        ids = {e.id for e in entries}
        assert "ok" in ids
        assert "bad-items" not in ids and "bad-type" not in ids
        assert "no-items" not in ids
        assert len(warnings) == 3

    def test_marketplace_install_bundle_rejected(self, tmp_path):
        from fusion_plugins_ecosystem.marketplace import Marketplace

        idx = tmp_path / "b.json"
        idx.write_text(
            json.dumps(
                {
                    "packages": [
                        {
                            "id": "b1",
                            "kind": "bundle",
                            "items": [{"type": "skill", "ref": "s"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        mp = Marketplace(connector_registry=None, marketplace_index=str(idx))
        with pytest.raises(ValueError, match="install_bundle"):
            mp.install("b1")


class TestEndpoints:
    @pytest.fixture()
    def client(self, populated):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fusion_plugins_ecosystem.console_app import create_app

        return TestClient(
            create_app(
                desk=populated.desk,
                registry=populated.registry,
                lifecycle=populated.lifecycle,
            )
        )

    async def test_full_flow_via_http(self, client, populated):
        r = client.post("/api/hub/bundles/finance-kit/install")
        assert r.status_code == 200
        assert r.json()["installed"] is True
        assert client.get("/api/hub/bundles").json()["bundles"]

        r = client.post(
            "/api/hub/bundles/finance-kit/items/excel-skill/tick",
            json={"enabled": False},
        )
        assert r.json()["state"] == "disabled"

        scopes = client.get("/api/hub/connectors/bank-api/oauth").json()
        assert any(s["sensitive"] for s in scopes["scopes"])
        r = client.post(
            "/api/hub/connectors/bank-api/oauth",
            json={"scopes": ["bank.flow.fetch"], "credentials": {"api_key": "k"}},
        )
        assert r.json()["completed"] is True
        assert client.get("/api/hub/connectors/bank-api/policy").json()[
            "per_use_scopes"
        ] == ["bank.order.push"]

        pv = client.get("/api/hub/bundles/finance-kit/uninstall-preview").json()
        assert pv["safe_to_remove"] is True
        rm = client.delete("/api/hub/bundles/finance-kit")
        assert rm.json()["removed"] is True

    def test_install_404(self, client):
        assert client.post("/api/hub/bundles/ghost/install").status_code == 404
