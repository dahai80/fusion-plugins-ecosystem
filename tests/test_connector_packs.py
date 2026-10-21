"""第一方连接器包目录测试（结构性校验，不联网）。"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.connector import ConnectorPack, RiskLevel
from fusion_plugins_ecosystem.connector_packs import (
    FEISHU_PACK,
    FIRST_PARTY_PACKS,
    NOTION_PACK,
    get_builtin_pack,
    list_builtin_packs,
)


def test_builtin_catalog_complete() -> None:
    assert set(FIRST_PARTY_PACKS) == {
        "feishu",
        "dingtalk",
        "wecom",
        "mail",
        "calendar",
        "notion",
    }
    assert len(list_builtin_packs()) == 6
    assert get_builtin_pack("feishu") is FEISHU_PACK
    assert get_builtin_pack("nope") is None


def test_all_packs_valid_constructors() -> None:
    # 构造期校验（transport/command/url/tools）全部通过
    for pack in list_builtin_packs():
        assert pack.id
        assert pack.version
        assert pack.description


def test_pack_ids_unique_and_tool_names_unique() -> None:
    for pack in list_builtin_packs():
        tool_names = [t.name for t in pack.tools]
        assert len(tool_names) == len(set(tool_names)), f"{pack.id} 工具名重复"
        assert tool_names, f"{pack.id} 无工具"


def test_tool_input_schema_shape() -> None:
    for pack in list_builtin_packs():
        for tool in pack.tools:
            schema = tool.input_schema
            assert schema.get("type") == "object"
            assert isinstance(schema.get("properties", {}), dict)
            assert isinstance(schema.get("required", []), list)
            assert set(schema["required"]).issubset(schema["properties"])


def test_credential_providers_declared() -> None:
    # 每个需要凭证的包都声明了 provider，env 目标名不冲突
    for pack in list_builtin_packs():
        providers = list(pack.env_credentials)
        targets = list(pack.env_credentials.values())
        assert len(targets) == len(set(targets)), f"{pack.id} env 目标重复"
        assert all(providers), f"{pack.id} 凭证 provider 为空"


def test_feishu_pack_acceptance_shape() -> None:
    # issue #27 验收：飞书可读消息/文档入工作流、结果回写、凭证本地存储
    tools = {t.name: t for t in FEISHU_PACK.tools}
    assert "im.message.list" in tools and tools["im.message.list"].read_only
    assert "docx.document.read" in tools
    send = tools["im.message.send"]
    assert send.risk == RiskLevel.HIGH and not send.read_only
    assert set(FEISHU_PACK.env_credentials) == {"feishu_app_id", "feishu_app_secret"}


def test_send_tools_gated_high_risk() -> None:
    # 所有「发送/写」类工具必须 HIGH 或 MEDIUM 风险，读类必须 LOW + read_only
    for pack in list_builtin_packs():
        for tool in pack.tools:
            if tool.read_only:
                assert tool.risk == RiskLevel.LOW, f"{pack.id}.{tool.name}"
            else:
                assert tool.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH), (
                    f"{pack.id}.{tool.name}"
                )


def test_notion_pack_http_transport() -> None:
    assert NOTION_PACK.transport == "http" and NOTION_PACK.url
    assert FEISHU_PACK.transport == "stdio" and FEISHU_PACK.command


def test_pack_roundtrip_all() -> None:
    for pack in list_builtin_packs():
        restored = ConnectorPack.from_dict(pack.to_dict())
        assert restored.id == pack.id
        assert [t.name for t in restored.tools] == [t.name for t in pack.tools]


def test_bridged_feishu_tools_register_into_registry() -> None:
    desk = fpe.DeskRuntime()
    creg = fpe.ConnectorRegistry(desk=desk)
    plugin_ids = creg.install_pack(FEISHU_PACK)
    assert len(plugin_ids) == len(FEISHU_PACK.tools)
    assert all(pid in desk.registered_plugin_ids for pid in plugin_ids)
    # 桥接插件可被 lifecycle 加载（INLINE 闭包入口）
    lifecycle = fpe.PluginLifecycle(creg.registry)
    for pid in plugin_ids:
        inst = lifecycle.load(pid)
        assert inst.state == fpe.PluginState.LOADED
