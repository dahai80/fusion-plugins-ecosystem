"""Authorization audit event stream tests (issue #43 PC-3).

Covers: install/OAuth/update/uninstall events into the fusion-security JSONL
pipeline (trace-id chaining), hub audit retrieval, scope revoke (re-bridge after
revoke + event), and the used-by reverse lookup endpoint.
"""

import json
import sys
from pathlib import Path

import pytest

from fusion_plugins_ecosystem import audit_events
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
    monkeypatch.setenv("FUSION_PLUGIN_AUDIT_SINK", "on")
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


def _actions(console, **kw):
    return [r["action"] for r in console.audit_records(**kw)["records"]]


class TestEventStream:
    async def test_install_oauth_update_uninstall_events(self, console, tmp_path):
        await console.install_bundle("finance-kit")
        assert "hub.bundle.install" in _actions(console, resource_id="finance-kit")
        assert "hub.bundle.item.install" in _actions(console, resource_id="excel-skill")

        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        # Per-scope grants + sensitive-policy events under one trace_id
        grants = console.audit_records(
            resource_id="bank-api", action="hub.connector.oauth.grant"
        )["records"]
        assert [g["details"]["scope"] for g in grants] == ["bank.flow.fetch"]
        policy = console.audit_records(
            resource_id="bank-api", action="hub.connector.scope.policy"
        )["records"]
        assert [p["details"]["scope"] for p in policy] == ["bank.order.push"]
        assert all(p["risk_level"] == "high" for p in policy)
        assert grants[0]["trace_id"] == policy[0]["trace_id"]

        # Update + uninstall events
        v2 = json.loads(json.dumps(BUNDLE))
        v2["version"] = "1.1.0"
        v2["items"].append({"type": "skill", "ref": "invoice-skill"})
        _write_index(tmp_path, [v2])
        console.registry.register(_manifest("invoice-skill", ["claude_skill"]))
        await console.bundle_update_apply("finance-kit", ["invoice-skill"])
        assert "hub.bundle.update" in _actions(console, resource_id="finance-kit")

        await console.uninstall_bundle("finance-kit")
        assert "hub.bundle.uninstall" in _actions(console, resource_id="finance-kit")

    async def test_disable_item_event(self, console):
        await console.install_bundle("finance-kit")
        await console.tick_bundle_item("finance-kit", "excel-skill", False)
        assert "hub.bundle.item.disable" in _actions(console, resource_id="excel-skill")

    def test_trace_id_link_across_items(self, console):
        trace = audit_events.new_trace_id()
        audit_events.emit("x.a", resource_id="r1", trace_id=trace)
        audit_events.emit("x.b", resource_id="r2", trace_id=trace)
        recs = console.audit_records(trace_id=trace)["records"]
        assert {r["action"] for r in recs} == {"x.a", "x.b"}

    def test_jsonl_contract_with_security_workbench(self, console, tmp_path):
        # Rows are shape-compatible with fusion_security AuditEntry (action/timestamp/risk_level/trace_id)
        audit_events.emit("hub.test", resource_id="r", risk_level="high")
        f = tmp_path / "audit" / "audit_system.jsonl"
        rec = json.loads(f.read_text(encoding="utf-8").splitlines()[-1])
        for key in (
            "action",
            "actor",
            "tenant_id",
            "resource_type",
            "resource_id",
            "details",
            "timestamp",
            "ip_address",
            "risk_level",
            "trace_id",
        ):
            assert key in rec, key
        assert rec["action"] == "hub.test" and rec["risk_level"] == "high"


class TestRevoke:
    async def test_revoke_scope_rebridges(self, console):
        await console.install_bundle("finance-kit")
        await console.oauth_complete(
            "bank-api", ["bank.flow.fetch", "bank.account.fetch"], {"api_key": "k"}
        )
        assert console.registry.get("connector__bank-api__bank.account.fetch")
        out = await console.revoke_scopes("bank-api", ["bank.account.fetch"])
        assert out["revoked"] is True
        assert out["remaining_scopes"] == ["bank.flow.fetch"]
        # Revoked channel no longer bridged, retained channels unaffected
        assert console.registry.get("connector__bank-api__bank.account.fetch") is None
        assert console.registry.get("connector__bank-api__bank.flow.fetch")
        assert "hub.connector.oauth.revoke" in _actions(console, resource_id="bank-api")
        # oauth_scopes reflects the actual grant state (used by the GUI revoke list / OAuth pre-check)
        flags = {
            s["id"]: s["granted"]
            for s in console.oauth_scopes("bank-api")["scopes"]
            if not s["sensitive"]
        }
        assert flags == {"bank.flow.fetch": True, "bank.account.fetch": False}

    async def test_revoke_all_disables(self, console):
        await console.install_bundle("finance-kit")
        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        out = await console.revoke_scopes("bank-api", ["bank.flow.fetch"])
        assert out["revoked"] is True and out["remaining_scopes"] == []
        assert console.registry.get("connector__bank-api__bank.flow.fetch") is None

    async def test_revoke_unknown_scope(self, console):
        await console.install_bundle("finance-kit")
        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        out = await console.revoke_scopes("bank-api", ["not-granted"])
        assert out["revoked"] is False

    async def test_remove_connector_clears_policy(self, console):
        from fusion_plugins_ecosystem.rest_connector import get_connector

        await console.install_bundle("finance-kit")
        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        out = await console.remove_connector("bank-api")
        assert out["removed"] is True
        assert get_connector("bank-api") is None
        assert console.registry.get("connector__bank-api__bank.flow.fetch") is None
        # Per-use policy cleared along with the connector
        assert console.scope_policy("bank-api")["per_use_scopes"] == []
        assert "hub.connector.remove" in _actions(console, resource_id="bank-api")
        out = await console.remove_connector("bank-api")
        assert out["removed"] is False
        # The _scope_policy metadata key must not leak into the bundle list
        listed = console.list_bundles()["bundles"]
        assert all(b.get("id") == "finance-kit" for b in listed)
        await console.uninstall_bundle("finance-kit")
        assert console.list_bundles() == {"bundles": []}


class TestUsedBy:
    async def test_used_by_endpoint_data(self, console):
        from test_install_flow import _FakeNode, _FakeNodeRegistry

        console.desk.node_registry = _FakeNodeRegistry(
            [_FakeNode("月度对账", "使用 node.excel-skill")]  # i18n: user-facing
        )
        out = console.capability_used_by("node.excel-skill")
        assert out["in_use_by"][0]["node"] == "月度对账"


class TestEndpoints:
    @pytest.fixture()
    def client(self, console):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fusion_plugins_ecosystem.console_app import create_app

        return TestClient(
            create_app(
                desk=console.desk,
                registry=console.registry,
                lifecycle=console.lifecycle,
            )
        )

    async def test_audit_and_revoke_via_http(self, client, console):
        await console.install_bundle("finance-kit")
        r = client.get("/api/hub/audit", params={"resource_id": "finance-kit"})
        assert r.status_code == 200
        assert any(x["action"] == "hub.bundle.install" for x in r.json()["records"])
        await console.oauth_complete("bank-api", ["bank.flow.fetch"], {"api_key": "k"})
        r = client.post(
            "/api/hub/connectors/bank-api/revoke",
            json={"scopes": ["bank.flow.fetch"]},
        )
        assert r.json()["revoked"] is True
        r = client.get("/api/hub/capabilities/node.excel-skill/used-by")
        assert r.json()["in_use_by"] == []
