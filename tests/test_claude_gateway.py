"""ClaudeGateway 全链路网关测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from fusion_plugins_ecosystem.claude_gateway import (
    CLAUDE_CODE,
    CLAUDE_DESKTOP,
    CLAUDE_VOLCENGINE,
    CLAUDE_WEB,
    ClaudeGateway,
    SubagentTask,
)
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)


# ── 测试 fixtures ──


def _make_manifest(
    plugin_id: str = "test_plugin",
    capabilities: list[PluginCapability] | None = None,
    default_mounted: bool = False,
    vram_mb: int = 0,
    timeout_seconds: int | None = None,
    entry_point: object = None,
) -> PluginManifest:
    caps = capabilities or [
        PluginCapability.MCP_TOOL,
        PluginCapability.CLAUDE_SKILL,
    ]
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description="测试插件",
        capabilities=caps,
        params=[
            PluginParam(
                name="text",
                type="string",
                description="输入",
                required=True,
            ),
        ],
        entry_point=entry_point,
        default_mounted=default_mounted,
        vram_mb=vram_mb,
        timeout_seconds=timeout_seconds,
    )


def _make_gateway(
    manifests: list[PluginManifest] | None = None,
    config: EcosystemConfig | None = None,
    desk: DeskRuntime | None = None,
) -> tuple[ClaudeGateway, PluginRegistry]:
    registry = PluginRegistry(desk=desk or DeskRuntime())
    for m in manifests or []:
        registry.register(m)
    lifecycle = PluginLifecycle(registry)
    gw = ClaudeGateway(
        registry=registry,
        lifecycle=lifecycle,
        config=config or EcosystemConfig(),
        desk=registry.desk,
    )
    return gw, registry


# ── 接入方式常量 ──


def test_claude_access_constants() -> None:
    assert CLAUDE_DESKTOP == "claude_desktop"
    assert CLAUDE_CODE == "claude_code"
    assert CLAUDE_WEB == "claude_web"
    assert CLAUDE_VOLCENGINE == "claude_volcengine"


# ── SubagentTask dataclass ──


def test_subagent_task_defaults() -> None:
    task = SubagentTask(name="t", plugin_id="p", arguments={"x": 1})
    assert task.name == "t"
    assert task.plugin_id == "p"
    assert task.arguments == {"x": 1}
    assert task.timeout_seconds is None
    assert task.metadata == {}


def test_subagent_task_with_metadata() -> None:
    task = SubagentTask(
        name="t",
        plugin_id="p",
        arguments={},
        timeout_seconds=30,
        metadata={"repo": "/path"},
    )
    assert task.timeout_seconds == 30
    assert task.metadata == {"repo": "/path"}


# ── export_skills ──


def test_export_skills_default_on() -> None:
    gw, _ = _make_gateway([_make_manifest("p1", default_mounted=True)])
    skills = gw.export_skills()
    assert len(skills) == 1
    assert skills[0]["name"] == "p1"


def test_export_skills_disabled_by_config() -> None:
    config = EcosystemConfig(auto_export_claude_skill=False)
    gw, _ = _make_gateway([_make_manifest("p1")], config=config)
    assert gw.export_skills() == []


def test_export_skills_empty_registry() -> None:
    gw, _ = _make_gateway()
    assert gw.export_skills() == []


# ── export_default_mounted_skills ──


def test_export_default_mounted_skills() -> None:
    gw, _ = _make_gateway(
        [
            _make_manifest("p1", default_mounted=True),
            _make_manifest("p2", default_mounted=False),
        ]
    )
    skills = gw.export_default_mounted_skills()
    assert len(skills) == 1


def test_export_default_mounted_disabled_by_config() -> None:
    config = EcosystemConfig(default_mount_compressor=False)
    gw, _ = _make_gateway([_make_manifest("p1", default_mounted=True)], config=config)
    assert gw.export_default_mounted_skills() == []


# ── list_mcp_tools ──


def test_list_mcp_tools_default_on() -> None:
    gw, _ = _make_gateway([_make_manifest("p1")])
    tools = gw.list_mcp_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "mcp__plugin__p1"


def test_list_mcp_tools_disabled_by_config() -> None:
    config = EcosystemConfig(enable_claude_mcp=False)
    gw, _ = _make_gateway([_make_manifest("p1")], config=config)
    assert gw.list_mcp_tools() == []


def test_list_mcp_tools_empty_registry() -> None:
    gw, _ = _make_gateway()
    assert gw.list_mcp_tools() == []


# ── invoke_mcp_tool ──


async def test_invoke_mcp_tool_success() -> None:
    def entry(_desk, params):
        return {"echo": params["text"]}

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    result = await gw.invoke_mcp_tool("p1", {"text": "hello"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"echo": "hello"}


async def test_invoke_mcp_tool_disabled_returns_error() -> None:
    config = EcosystemConfig(enable_claude_mcp=False)
    gw, _ = _make_gateway([_make_manifest("p1")], config=config)
    result = await gw.invoke_mcp_tool("p1", {"text": "x"})
    assert result["isError"] is True
    assert "MCP" in result["content"][0]["text"]


async def test_invoke_mcp_tool_unknown_plugin_returns_error() -> None:
    gw, _ = _make_gateway()
    result = await gw.invoke_mcp_tool("nonexistent", {})
    assert result["isError"] is True
    assert "未注册" in result["content"][0]["text"]


async def test_invoke_mcp_tool_no_mcp_capability_returns_error() -> None:
    gw, _ = _make_gateway(
        [
            _make_manifest(
                "p1",
                capabilities=[PluginCapability.CLAUDE_SKILL],
            )
        ]
    )
    result = await gw.invoke_mcp_tool("p1", {})
    assert result["isError"] is True
    assert "不具备" in result["content"][0]["text"]


async def test_invoke_mcp_tool_crash_returns_error() -> None:
    def entry(_desk, _params):
        raise RuntimeError("boom")

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    result = await gw.invoke_mcp_tool("p1", {})
    assert result["isError"] is True
    assert "执行失败" in result["content"][0]["text"]


async def test_invoke_mcp_tool_non_dict_result_serialized() -> None:
    def entry(_desk, _params):
        return "plain string result"

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    result = await gw.invoke_mcp_tool("p1", {})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "plain string result"


# ── gateway_info ──


def test_gateway_info_contains_metadata() -> None:
    gw, _ = _make_gateway([_make_manifest("p1", default_mounted=True)])
    info = gw.gateway_info()
    assert "transport" in info
    assert "port" in info
    assert "protocol_version" in info
    assert info["skills_count"] == 1
    assert info["default_mounted_count"] == 1
    assert "config" in info
    assert isinstance(info["config"], dict)


def test_gateway_info_empty_registry() -> None:
    gw, _ = _make_gateway()
    info = gw.gateway_info()
    assert info["skills_count"] == 0
    assert info["default_mounted_count"] == 0


# ── dispatch_subagent ──


async def test_dispatch_subagent_success() -> None:
    def entry(_desk, params):
        return {"refactored": params["files"]}

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    task = SubagentTask(
        name="batch-refactor",
        plugin_id="p1",
        arguments={"files": ["a.py", "b.py"]},
    )
    result = await gw.dispatch_subagent(task)
    assert result["state"] == "completed"
    assert result["task"] == "batch-refactor"
    assert result["result"] == {"refactored": ["a.py", "b.py"]}


async def test_dispatch_subagent_unknown_plugin_raises() -> None:
    gw, _ = _make_gateway()
    task = SubagentTask(name="t", plugin_id="nonexistent", arguments={})
    with pytest.raises(KeyError, match="未注册"):
        await gw.dispatch_subagent(task)


async def test_dispatch_subagent_crash_returns_failed() -> None:
    def entry(_desk, _params):
        raise RuntimeError("boom")

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    task = SubagentTask(name="t", plugin_id="p1", arguments={})
    result = await gw.dispatch_subagent(task)
    assert result["state"] == "failed"
    assert "boom" in result["error"]


async def test_dispatch_subagent_timeout_destroy_on() -> None:
    async def entry(_desk, _params):
        await asyncio.sleep(10)
        return {}

    config = EcosystemConfig(subagent_timeout_seconds=0.1)
    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)], config=config)
    task = SubagentTask(name="t", plugin_id="p1", arguments={})
    result = await gw.dispatch_subagent(task)
    assert result["state"] == "failed"
    # 超时销毁：插件应被 unload
    assert "p1" not in gw.lifecycle._instances


async def test_dispatch_subagent_timeout_destroy_off() -> None:
    async def entry(_desk, _params):
        await asyncio.sleep(10)
        return {}

    config = EcosystemConfig(
        subagent_timeout_seconds=0.1,
        subagent_timeout_destroy=False,
    )
    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)], config=config)
    task = SubagentTask(name="t", plugin_id="p1", arguments={})
    result = await gw.dispatch_subagent(task)
    assert result["state"] == "failed"
    # 销毁关闭：插件仍保留（可能为 CRASHED/TIMEOUT 状态）
    assert "p1" in gw.lifecycle._instances


async def test_dispatch_subagent_preserves_manifest_timeout() -> None:
    called = []

    def entry(_desk, _params):
        called.append(1)
        return {}

    gw, _ = _make_gateway(
        [_make_manifest("p1", entry_point=entry, timeout_seconds=120)]
    )
    original = gw.registry.get("p1").timeout_seconds
    task = SubagentTask(
        name="t",
        plugin_id="p1",
        arguments={},
        timeout_seconds=5,
    )
    await gw.dispatch_subagent(task)
    # 任务结束后 manifest.timeout_seconds 应恢复
    assert gw.registry.get("p1").timeout_seconds == original


# ── list_subagent_capable_plugins ──


def test_list_subagent_capable_plugins() -> None:
    gw, _ = _make_gateway(
        [
            _make_manifest(
                "p1",
                capabilities=[
                    PluginCapability.SUBAGENT,
                    PluginCapability.MCP_TOOL,
                ],
            ),
            _make_manifest("p2"),
        ]
    )
    capable = gw.list_subagent_capable_plugins()
    assert capable == ["p1"]


def test_list_subagent_capable_plugins_empty() -> None:
    gw, _ = _make_gateway()
    assert gw.list_subagent_capable_plugins() == []


# ── store_credentials / get_credentials / has_credentials ──


class _FakeConfigCenter:
    """Desk 配置中心 fake，支持 get/set API 密钥。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


def _make_desk_with_config() -> DeskRuntime:
    """构造带 config_center 的 DeskRuntime，供鉴权测试。"""
    return DeskRuntime(config_center=_FakeConfigCenter())


def test_store_credentials_writes_to_desk() -> None:
    desk = _make_desk_with_config()
    gw, _ = _make_gateway([], desk=desk)
    gw.store_credentials("anthropic", "sk-test-123")
    assert gw.get_credentials("anthropic") == "sk-test-123"


def test_store_credentials_volcengine_disabled_by_config() -> None:
    config = EcosystemConfig(enable_volcengine_claude_plan=False)
    desk = _make_desk_with_config()
    gw, _ = _make_gateway([], config=config, desk=desk)
    gw.store_credentials("volcengine_claude", "sk-vol-123")
    # 关闭时应未存储
    assert gw.get_credentials("volcengine_claude") is None


def test_store_credentials_volcengine_enabled() -> None:
    desk = _make_desk_with_config()
    gw, _ = _make_gateway([], desk=desk)
    gw.store_credentials("volcengine_claude", "sk-vol-456")
    assert gw.get_credentials("volcengine_claude") == "sk-vol-456"


def test_get_credentials_unknown_returns_none() -> None:
    gw, _ = _make_gateway()
    assert gw.get_credentials("unknown") is None


def test_has_credentials_true_after_store() -> None:
    desk = _make_desk_with_config()
    gw, _ = _make_gateway([], desk=desk)
    gw.store_credentials("anthropic", "sk-x")
    assert gw.has_credentials("anthropic") is True


def test_has_credentials_false_when_absent() -> None:
    gw, _ = _make_gateway()
    assert gw.has_credentials("anthropic") is False


def test_has_credentials_volcengine_disabled() -> None:
    config = EcosystemConfig(enable_volcengine_claude_plan=False)
    desk = _make_desk_with_config()
    gw, _ = _make_gateway([], config=config, desk=desk)
    gw.store_credentials("volcengine_claude", "sk-x")
    assert gw.has_credentials("volcengine_claude") is False


# ── mlx_visual_backend ──


class _FakeMLXClient:
    async def chat(self, model, messages, **kw):
        return {"content": "fake", "model": model}


async def test_mlx_visual_backend_success() -> None:
    desk = DeskRuntime(mlx_client=_FakeMLXClient())
    gw, _ = _make_gateway([], desk=desk)
    result = await gw.mlx_visual_backend("qwen3.5", [{"role": "user", "content": "hi"}])
    assert result["content"] == "fake"


async def test_mlx_visual_backend_disabled_raises() -> None:
    config = EcosystemConfig(enable_mixed_quantization=False)
    desk = DeskRuntime(mlx_client=_FakeMLXClient())
    gw, _ = _make_gateway([], config=config, desk=desk)
    with pytest.raises(RuntimeError, match="混合量化"):
        await gw.mlx_visual_backend("m", [])


# ── 与 caveman 内置插件集成 ──


def test_gateway_with_builtin_caveman() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    gw = ClaudeGateway(registry=registry, lifecycle=PluginLifecycle(registry))
    skills = gw.export_skills()
    assert len(skills) == 1
    assert skills[0]["name"] == "caveman_compress"
    mcp_tools = gw.list_mcp_tools()
    assert len(mcp_tools) == 1
    assert mcp_tools[0]["name"] == "mcp__plugin__caveman_compress"


async def test_gateway_invoke_caveman_via_mcp() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    gw = ClaudeGateway(registry=registry, lifecycle=PluginLifecycle(registry))
    result = await gw.invoke_mcp_tool(
        "caveman_compress",
        {"text": "# comment\ncode"},
    )
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["compressed"] == "code"
    assert payload["ratio"] < 1.0


async def test_gateway_dispatch_caveman_subagent() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    gw = ClaudeGateway(registry=registry, lifecycle=PluginLifecycle(registry))
    task = SubagentTask(
        name="compress-session",
        plugin_id="caveman_compress",
        arguments={"text": "a\n\n\n\nb"},
    )
    result = await gw.dispatch_subagent(task)
    assert result["state"] == "completed"
    assert result["result"]["compressed"] == "a\n\nb"


# ── 构造器默认参数 ──


def test_gateway_defaults_lifecycle_and_desk() -> None:
    registry = PluginRegistry()
    registry.register_builtin()
    gw = ClaudeGateway(registry=registry)
    assert gw.lifecycle is not None
    assert gw.desk is registry.desk
    assert gw.config.enable_claude_mcp is True
    assert gw.token_meter is not None


def test_gateway_uses_registry_desk_when_not_passed() -> None:
    registry = PluginRegistry()
    gw = ClaudeGateway(registry=registry)
    assert gw.desk is registry.desk


def test_gateway_explicit_desk_overrides() -> None:
    registry = PluginRegistry()
    desk2 = DeskRuntime()
    gw = ClaudeGateway(registry=registry, desk=desk2)
    assert gw.desk is desk2


# ── token meter 集成 ──


async def test_invoke_mcp_tool_records_mcp_relay_token() -> None:
    def entry(_desk, _params):
        return {"ok": True}

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    await gw.invoke_mcp_tool("p1", {})
    records = gw.token_meter.records_for("p1")
    assert len(records) >= 1
    # 应至少有一条 MCP_RELAY 记录
    kinds = [r.kind.value for r in records]
    assert "mcp_relay" in kinds


async def test_dispatch_subagent_records_plugin_local_token() -> None:
    def entry(_desk, _params):
        return {}

    gw, _ = _make_gateway([_make_manifest("p1", entry_point=entry)])
    task = SubagentTask(name="t", plugin_id="p1", arguments={})
    await gw.dispatch_subagent(task)
    records = gw.token_meter.records_for("p1")
    assert len(records) >= 1
    kinds = [r.kind.value for r in records]
    assert "plugin_local" in kinds


async def test_mlx_visual_backend_records_mlx_inference_token() -> None:
    desk = DeskRuntime(mlx_client=_FakeMLXClient())
    gw, _ = _make_gateway([], desk=desk)
    await gw.mlx_visual_backend("m", [])
    records = gw.token_meter.records_for("mlx_visual_backend")
    assert len(records) == 1
    assert records[0].kind.value == "mlx_inference"
