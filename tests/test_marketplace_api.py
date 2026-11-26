# Marketplace backend API tests (issue #74): listing/detail/permissions,
# per-tenant install state + idempotency, publish/review queue, endpoint wiring.

import sys
from pathlib import Path

import pytest

from fusion_plugins_ecosystem.extensions_console import ExtensionsConsole
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.marketplace_api import PluginMarketplaceAPI
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_extensions_console import FakeDesk  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    monkeypatch.delenv("FUSION_PLUGINS_CONSOLE_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_PLUGIN_MARKETPLACE_INDEX", raising=False)
    from fusion_plugins_ecosystem import shell_registry

    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key")


@pytest.fixture()
def api():
    desk = FakeDesk()
    registry = PluginRegistry(desk)
    lifecycle = PluginLifecycle(registry)
    console = ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)
    return PluginMarketplaceAPI(console)


def _register_plugin(registry, pid="ctx_tool", version="1.0.0", caps=("mcp_tool",)):
    manifest = PluginManifest(
        id=pid,
        name=pid,
        version=version,
        category=PluginCategory.CUSTOM,
        description="test plugin",
        capabilities=tuple(PluginCapability(c) for c in caps),
        entry_point="json:dump",
    )
    registry.register(manifest)
    return manifest


class TestListing:
    async def test_listing_contains_registry_plugin_with_catalog_meta(self, api):
        out = api.list_plugins()
        ids = [p["id"] for p in out["plugins"]]
        assert "caveman_compress" in ids
        row = next(p for p in out["plugins"] if p["id"] == "caveman_compress")
        assert row["official"] is True
        assert row["lifecycle_stage"] == "stable"
        assert row["name_cn"]
        assert row["install_state"] == "not_installed"
        assert row["source_module"] == "fusion-plugins-ecosystem"

    async def test_listing_contains_builtin_connector_packs(self, api):
        out = api.list_plugins()
        ids = {p["id"] for p in out["plugins"]}
        assert {"feishu", "dingtalk", "wecom", "mail", "calendar", "notion"} <= ids
        feishu = next(p for p in out["plugins"] if p["id"] == "feishu")
        assert feishu["kind"] == "connector_pack"
        assert any(p["id"] == "network" for p in feishu["permissions"])

    async def test_listing_search_and_category_filter(self, api):
        out = api.list_plugins(query="feishu")
        assert out["count"] >= 1
        assert all("feishu" in p["id"] or any("feishu" in t for t in p["tags"]) for p in out["plugins"])
        out2 = api.list_plugins(category="connector")
        assert out2["count"] >= 6
        assert all(p["category"] == "connector" for p in out2["plugins"])

    async def test_listing_invalid_tenant_rejected(self, api):
        with pytest.raises(ValueError):
            api.list_plugins(tenant="bad tenant!")


class TestDetailPermissions:
    async def test_detail_derives_permissions_from_capabilities(self, api):
        _register_plugin(api.console.registry, caps=("file_access", "mcp_tool", "vram_consumer"))
        detail = api.plugin_detail("ctx_tool")
        perm_ids = {p["id"] for p in detail["permissions"]}
        assert {"file_access", "network", "vram"} <= perm_ids
        for p in detail["permissions"]:
            assert {"id", "label_en", "label_cn", "risk"} <= set(p)

    async def test_detail_connector_pack_surfaces_network_risk(self, api):
        detail = api.plugin_detail("feishu")
        assert any(p["id"] == "network" for p in detail["permissions"])

    async def test_detail_unknown_plugin_raises(self, api):
        with pytest.raises(KeyError):
            api.plugin_detail("nope")


class TestInstallState:
    async def test_install_uninstall_registry_plugin_lifecycle(self, api):
        _register_plugin(api.console.registry)
        out = await api.install("ctx_tool", tenant="acme")
        assert out["installed"] is True and out["already"] is False
        assert api.console._plugin_enabled("ctx_tool") is True
        row = next(p for p in api.list_plugins(tenant="acme")["plugins"] if p["id"] == "ctx_tool")
        assert row["install_state"] == "installed"
        assert row["installed_version"] == "1.0.0"
        # tenant record bumps the installs counter
        assert row["installs"] >= 1

        out2 = await api.install("ctx_tool", tenant="acme")
        assert out2["already"] is True

        un = await api.uninstall("ctx_tool", tenant="acme")
        assert un["uninstalled"] is True
        row2 = next(p for p in api.list_plugins(tenant="acme")["plugins"] if p["id"] == "ctx_tool")
        assert row2["install_state"] == "not_installed"

    async def test_tenants_isolated(self, api):
        _register_plugin(api.console.registry)
        await api.install("ctx_tool", tenant="acme")
        # registry-plugin enable is machine-global live truth, so tenant
        # "other" also sees installed (with no version record of its own);
        # the version/installs record stays per-tenant
        row = next(p for p in api.list_plugins(tenant="other")["plugins"] if p["id"] == "ctx_tool")
        assert row["install_state"] == "installed"
        assert row["installed_version"] == ""
        acme = next(p for p in api.list_plugins(tenant="acme")["plugins"] if p["id"] == "ctx_tool")
        assert acme["installed_version"] == "1.0.0"
        assert acme["installs"] == 1

    async def test_update_available_when_recorded_version_older(self, api):
        _register_plugin(api.console.registry)
        api._record_install("acme", "ctx_tool", "0.9.0", enabled=True)
        row = next(p for p in api.list_plugins(tenant="acme")["plugins"] if p["id"] == "ctx_tool")
        assert row["install_state"] == "update_available"
        assert row["installed_version"] == "0.9.0"

    async def test_install_market_entry_idempotent(self, api):
        out = await api.install("feishu", tenant="acme")
        assert out["installed"] is True
        bundles = api.console.list_bundles()["bundles"]
        assert any(b["id"] == "feishu" for b in bundles)
        out2 = await api.install("feishu", tenant="acme")
        assert out2["already"] is True
        un = await api.uninstall("feishu", tenant="acme")
        assert un["uninstalled"] is True
        assert not any(b["id"] == "feishu" for b in api.console.list_bundles()["bundles"])

    async def test_install_unknown_raises(self, api):
        with pytest.raises(KeyError):
            await api.install("nope", tenant="acme")


class TestPublishReview:
    def _manifest(self, pid="community_skill", version="1.0.0"):
        return {
            "id": pid,
            "name": "Community Skill",
            "version": version,
            "description": "A community contributed skill",
            "category": "custom",
            "source_repo": "https://github.com/example/community_skill",
            "permissions": [{"id": "file_access", "risk": "medium"}],
        }

    async def test_submit_validate_review_approve_flows_into_listing(self, api):
        out = api.submit(self._manifest(), submitter="dev1")
        assert out["submitted"] is True and out["status"] == "pending"
        queue = api.list_submissions(status="pending")
        assert queue["count"] == 1
        assert queue["submissions"][0]["submission_id"] == "community_skill@1.0.0"

        rev = api.review("community_skill@1.0.0", approve=True, reviewer="admin")
        assert rev["status"] == "approved"

        listing = api.list_plugins()
        row = next(p for p in listing["plugins"] if p["id"] == "community_skill")
        assert row["official"] is False
        assert row["lifecycle_stage"] == "beta"
        assert row["source_module"] == "https://github.com/example/community_skill"
        # regression: community manifests carry plugin-manifest key names —
        # they must map onto the catalog keys (name_en/description_en/version)
        assert row["name_en"] == "Community Skill"
        assert row["description_en"] == "A community contributed skill"
        assert row["version"] == "1.0.0"
        assert row["permissions"] == [{"id": "file_access", "risk": "medium"}]

    async def test_reject_path_and_double_review_guard(self, api):
        api.submit(self._manifest())
        rev = api.review("community_skill@1.0.0", approve=False)
        assert rev["status"] == "rejected"
        with pytest.raises(ValueError):
            api.review("community_skill@1.0.0", approve=True)
        listing = api.list_plugins()
        assert all(p["id"] != "community_skill" for p in listing["plugins"])

    async def test_invalid_manifest_rejected(self, api):
        bad = self._manifest(pid="Bad Id!")
        with pytest.raises(ValueError):
            api.submit(bad)
        bad2 = self._manifest()
        bad2["category"] = "not_a_category"
        with pytest.raises(ValueError):
            api.submit(bad2)
        bad3 = self._manifest()
        bad3["source_repo"] = "http://insecure.example/x"
        with pytest.raises(ValueError):
            api.submit(bad3)
        bad4 = self._manifest()
        bad4["version"] = "1.0"
        bad4["id"] = "ok_id"
        # 1.0 is a valid version shape — should succeed
        assert api.submit(bad4)["submitted"] is True

    async def test_resubmission_replaces_pending(self, api):
        api.submit(self._manifest())
        m2 = self._manifest()
        m2["description"] = "updated description"
        out = api.submit(m2)
        assert out["submitted"] is True
        queue = api.list_submissions(status="pending")
        assert queue["count"] == 1
        assert queue["submissions"][0]["manifest"]["description"] == "updated description"

    async def test_duplicate_publish_blocked(self, api):
        api.submit(self._manifest())
        api.review("community_skill@1.0.0", approve=True)
        with pytest.raises(ValueError):
            api.submit(self._manifest())

    async def test_review_unknown_submission(self, api):
        with pytest.raises(KeyError):
            api.review("ghost@1.0.0", approve=True)


class TestEndpoints:
    @pytest.fixture()
    def client(self, api, monkeypatch):
        from fastapi.testclient import TestClient

        from fusion_plugins_ecosystem import console_app

        app = console_app.create_app(
            desk=api.console.desk,
            registry=api.console.registry,
            lifecycle=api.console.lifecycle,
        )
        return TestClient(app)

    def test_get_plugins_endpoint(self, client):
        resp = client.get("/api/marketplace/plugins", headers={"X-Tenant": "acme"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant"] == "acme"
        assert any(p["id"] == "caveman_compress" for p in body["plugins"])

    def test_detail_404(self, client):
        resp = client.get("/api/marketplace/plugins/nope")
        assert resp.status_code == 404

    def test_submit_and_review_endpoints(self, client):
        resp = client.post(
            "/api/marketplace/submissions",
            json={
                "submitter": "dev1",
                "manifest": {
                    "id": "end_to_end_plugin",
                    "name": "E2E",
                    "version": "1.0.0",
                    "description": "e2e",
                    "category": "custom",
                    "source_repo": "https://github.com/example/e2e",
                },
            },
        )
        assert resp.status_code == 200
        sid = resp.json()["submission_id"]
        resp2 = client.post(f"/api/marketplace/submissions/{sid}/review", json={"approve": True})
        assert resp2.status_code == 200
        listing = client.get("/api/marketplace/plugins").json()
        assert any(p["id"] == "end_to_end_plugin" for p in listing["plugins"])

    def test_bad_submission_422(self, client):
        resp = client.post(
            "/api/marketplace/submissions",
            json={"manifest": {"id": "x!", "name": "", "version": "", "description": ""}},
        )
        assert resp.status_code == 422
