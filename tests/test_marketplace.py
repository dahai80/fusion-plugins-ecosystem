"""marketplace 测试：内置目录/索引加载/安装卸载/安全约束。"""

from __future__ import annotations

import json

import pytest

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.connector import ConnectorRegistry
from fusion_plugins_ecosystem.marketplace import Marketplace


@pytest.fixture()
def setup(tmp_path):
    creg = ConnectorRegistry()
    market = Marketplace(creg, install_root=str(tmp_path / "mkt"))
    return creg, market


def test_builtin_catalog_six_entries(setup) -> None:
    _, market = setup
    catalog = market.builtin_catalog()
    assert {e.id for e in catalog} == {
        "feishu",
        "dingtalk",
        "wecom",
        "mail",
        "calendar",
        "notion",
    }
    assert all(e.kind == "connector_pack" for e in catalog)
    assert all(e.pack is not None for e in catalog)


def test_load_index_builtin_only_no_source(setup) -> None:
    _, market = setup
    entries, warnings = market.load_index()
    assert len(entries) == 6
    assert warnings == []


def test_load_index_local_file_merges_and_warns(tmp_path, setup) -> None:
    _, market = setup
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "id": "feishu",
                        "kind": "connector_pack",
                        "name": "飞书覆盖版",
                        "version": "9.9.9",
                        "pack": {
                            "id": "feishu",
                            "transport": "stdio",
                            "command": ["echo", "x"],
                            "tools": [{"name": "t", "description": ""}],
                        },
                    },
                    {"id": "bad", "kind": "unknown_kind"},
                    {"kind": "connector_pack"},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries, warnings = market.load_index(str(index))
    ids = [e.id for e in entries]
    assert "feishu" in ids
    assert len(warnings) == 2
    overridden = next(e for e in entries if e.id == "feishu")
    assert overridden.version == "9.9.9"


def test_load_index_http_source_rejected(setup) -> None:
    _, market = setup
    entries, warnings = market.load_index("http://evil.example.com/index.json")
    assert len(entries) == 6  # 内置目录兜底
    assert any("HTTPS" in w for w in warnings)


def test_load_index_unreachable_source_warns(setup) -> None:
    _, market = setup
    entries, warnings = market.load_index("/nonexistent/index.json")
    assert len(entries) == 6
    assert warnings and "索引加载失败" in warnings[0]


def test_install_builtin_connector_pack(setup) -> None:
    creg, market = setup
    result = market.install("feishu")
    assert result["kind"] == "connector_pack"
    assert len(result["plugin_ids"]) == len(creg.get_pack("feishu").tools)
    assert creg.get_pack("feishu") is not None


def test_install_from_local_source_pack_json(tmp_path, setup) -> None:
    creg, market = setup
    src = tmp_path / "mypack"
    src.mkdir()
    (src / "pack.json").write_text(
        json.dumps(
            {
                "id": "custom_pack",
                "name": "Custom",
                "version": "1.0.0",
                "description": "",
                "transport": "stdio",
                "command": ["echo", "x"],
                "tools": [{"name": "t", "description": "", "risk": "low"}],
            }
        ),
        encoding="utf-8",
    )
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "packages": [
                    {"id": "custom_pack", "kind": "connector_pack", "source": str(src)}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = market.install("custom_pack", source=str(index))
    assert result["plugin_ids"] == ["connector__custom_pack__t"]
    assert creg.get_pack("custom_pack").name == "Custom"


def test_install_source_missing_pack_json_raises(tmp_path, setup) -> None:
    _, market = setup
    src = tmp_path / "empty"
    src.mkdir()
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {"packages": [{"id": "e1", "kind": "connector_pack", "source": str(src)}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pack.json"):
        market.install("e1", source=str(index))


def test_install_skill_package_copies_tree(tmp_path, setup) -> None:
    _, market = setup
    src = tmp_path / "skill-src"
    src.mkdir()
    (src / "SKILL.md").write_text("# demo skill", encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "packages": [
                    {"id": "demo_skill", "kind": "skill_package", "source": str(src)}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = market.install("demo_skill", source=str(index))
    assert result["kind"] == "skill_package"
    assert (tmp_path / "mkt" / "demo_skill" / "SKILL.md").exists()


def test_install_unknown_entry_raises(setup) -> None:
    _, market = setup
    with pytest.raises(KeyError):
        market.install("ghost")


def test_uninstall_removes_pack_and_skill_dir(tmp_path, setup) -> None:
    creg, market = setup
    market.install("feishu")
    src = tmp_path / "skill-src"
    src.mkdir()
    (src / "SKILL.md").write_text("x", encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {"packages": [{"id": "s1", "kind": "skill_package", "source": str(src)}]}
        ),
        encoding="utf-8",
    )
    market.install("s1", source=str(index))
    assert creg.get_pack("feishu") is not None

    market.uninstall("feishu")
    market.uninstall("s1")
    assert creg.get_pack("feishu") is None
    assert creg.registry.get("connector__feishu__im.message.list") is None
    assert not (tmp_path / "mkt" / "s1").exists()


def test_list_installed(setup) -> None:
    _, market = setup
    market.install("feishu")
    installed = market.list_installed()
    kinds = {(i["kind"], i["id"]) for i in installed}
    assert ("connector_pack", "feishu") in kinds


def test_marketplace_index_config_wiring() -> None:
    # 配置面板字段存在且可序列化回环
    cfg = fpe.EcosystemConfig(
        connector_marketplace_index="https://example.com/index.json",
        connector_persist_path="/tmp/c.json",
    )
    data = cfg.to_dict()
    assert data["enable_connector_framework"] is True
    assert data["connector_marketplace_index"].startswith("https://")
    cfg2, warnings = fpe.EcosystemConfig.from_dict(data)
    assert not warnings
    assert cfg2.connector_persist_path == "/tmp/c.json"
