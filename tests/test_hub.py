"""能力中心 hub 测试（issue #41：四 tab + 分组 + 跨类搜索）。

覆盖: 内置插件自举、四 tab 分类（skill/subagent 能力归属）、
跨类搜索、市场分组（推荐/更新版本比较）、/api/hub 端点。
"""

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

sys.path.insert(0, str(Path(__file__).parent))
from test_extensions_console import FakeDesk  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    # 密钥隔离: 真实部署可能存在 ~/.fusion-plugins-ecosystem/web_module_key
    monkeypatch.delenv("FUSION_PLUGINS_CONSOLE_API_KEY", raising=False)
    from fusion_plugins_ecosystem import shell_registry

    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key")


@pytest.fixture()
def console():
    desk = FakeDesk()
    registry = PluginRegistry(desk)
    lifecycle = PluginLifecycle(registry)
    return ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)


def _manifest(pid, caps, name=None, description=""):
    return PluginManifest(
        id=pid,
        name=name or pid,
        version="1.0.0",
        category=PluginCategory.CUSTOM,
        description=description,
        capabilities=tuple(PluginCapability(c) for c in caps),
        entry_point="json:dump",
    )


class TestBuiltinBootstrap:
    def test_builtin_registered_on_init(self, console):
        # issue #41：hub 面反映真实注册表 — console 自举内置插件
        assert console.registry.get("caveman_compress") is not None

    def test_builtin_idempotent_on_injected_registry(self, console):
        # 注入侧已注册时重复构造不炸（register 同版本幂等）
        console2 = ExtensionsConsole(
            desk=console.desk,
            registry=console.registry,
            lifecycle=console.lifecycle,
        )
        assert console2.registry.get("caveman_compress") is not None


class TestTabs:
    def test_builtin_skill_in_skills_tab(self, console):
        hub = console.list_hub()
        skills = hub["tabs"]["skills"]
        assert any(s["id"] == "caveman_compress" for s in skills)
        assert any(s["id"] == "caveman_compress" for s in hub["tabs"]["plugins"])

    def test_subagent_tab_classification(self, console):
        console.registry.register(
            _manifest("research-sub", ["subagent"], description="差异研究员")
        )
        hub = console.list_hub()
        assert any(s["id"] == "research-sub" for s in hub["tabs"]["subagents"])
        assert not any(s["id"] == "research-sub" for s in hub["tabs"]["skills"])

    def test_bridge_ids_not_duplicated(self, console):
        console.registry.register(
            _manifest("connector__x__op", ["mcp_tool"]),
        )
        hub = console.list_hub()
        all_ids = [i["id"] for tab in hub["tabs"].values() for i in tab]
        assert "connector__x__op" not in all_ids

    def test_counts_match_tabs(self, console):
        hub = console.list_hub()
        assert hub["counts"] == {k: len(v) for k, v in hub["tabs"].items()}


class TestSearch:
    async def test_cross_type_search(self, console):
        console.registry.register(
            _manifest("excel-skill", ["claude_skill"], description="Excel 专家")
        )
        console.registry.register(
            _manifest("bank-conn-agent", ["subagent"], description="网银对账子代理")
        )
        hub = console.list_hub("excel")
        assert [i["id"] for i in hub["tabs"]["skills"]] == ["excel-skill"]
        assert hub["tabs"]["subagents"] == []
        hub = console.list_hub("对账")
        assert any(i["id"] == "bank-conn-agent" for i in hub["tabs"]["subagents"])

    def test_search_case_insensitive(self, console):
        hub = console.list_hub("CAVEMAN")
        assert any(i["id"] == "caveman_compress" for i in hub["tabs"]["skills"])

    def test_search_hits_recommended_connector_pack(self, console):
        # 未装连接器时搜索也能命中连接器类型 — 推荐位市场条目入搜索面
        hub = console.list_hub("notion")
        hits = hub["tabs"]["connectors"]
        assert any(i["id"] == "notion" and i["recommended"] for i in hits)
        assert all(not i.get("recommended") or i["enabled"] is False for i in hits)
        # 空查询不把推荐位混进 tab（counts 保持纯已装口径）
        full = console.list_hub()
        assert all(
            not i.get("recommended") for tab in full["tabs"].values() for i in tab
        )

    def test_empty_query_returns_all(self, console):
        full = console.list_hub()
        hub = console.list_hub("")
        assert hub["counts"] == full["counts"]


class TestGroups:
    def test_recommended_has_builtin_packs(self, console):
        # 第一方连接器包目录未安装 → 推荐位非空
        hub = console.list_hub()
        rec_ids = {r["id"] for r in hub["groups"]["recommended"]}
        assert rec_ids, "builtin pack catalog should surface as recommended"

    def test_updates_version_compare(self, console):
        assert console._version_key("1.2") < console._version_key("1.10")
        assert console._version_key("1.0.0") == console._version_key("1.0")
        assert console._version_key("v2") > console._version_key("1.9.9")

    def test_installed_group_lists_registry_entries(self, console):
        hub = console.list_hub()
        ids = {i["id"] for i in hub["groups"]["installed"]}
        assert "caveman_compress" in ids

    async def test_enable_reflected_in_groups(self, console):
        await console.tick_nodes("caveman_compress", ["caveman_compress"])
        hub = console.list_hub()
        entry = next(
            i for i in hub["groups"]["installed"] if i["id"] == "caveman_compress"
        )
        assert entry["enabled"] is True
        skill = next(s for s in hub["tabs"]["skills"] if s["id"] == "caveman_compress")
        assert skill["enabled"] is True


class TestHubEndpoint:
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

    def test_hub_endpoint_shape(self, client):
        r = client.get("/api/hub")
        assert r.status_code == 200
        data = r.json()
        assert set(data["tabs"]) == {"plugins", "skills", "connectors", "subagents"}
        assert set(data["groups"]) == {"installed", "updates", "recommended"}

    def test_hub_endpoint_query_param(self, client):
        r = client.get("/api/hub", params={"q": "caveman"})
        data = r.json()
        assert data["query"] == "caveman"
        assert any(i["id"] == "caveman_compress" for i in data["tabs"]["skills"])

    def test_static_page_has_new_tabs(self, client):
        r = client.get("/")
        assert 'data-tab="skills"' in r.text
        assert 'data-tab="subagents"' in r.text
        assert 'id="hub-search"' in r.text
