"""集成测试 — 端到端流程验证。"""

from __future__ import annotations

import tempfile
from pathlib import Path


from fusion_plugins_ecosystem.agent_adapter import AgentAdapter
from fusion_plugins_ecosystem.claude_gateway import ClaudeGateway
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import MCPHandler
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
from fusion_plugins_ecosystem.mcp_exporter import MCPExporter
from fusion_plugins_ecosystem.plugin_bundle import PluginBundleGenerator
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
    PluginRegistry,
)
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode
from fusion_plugins_ecosystem.skill_adapter import SkillAdapter
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter, TokenRecord


def _compressor_manifest() -> PluginManifest:
    return PluginManifest(
        id="caveman_compress",
        name="Caveman Compressor",
        version="1.0.0",
        category=PluginCategory.CONTEXT_COMPRESS,
        description="代码上下文压缩",
        capabilities=(
            PluginCapability.MCP_TOOL,
            PluginCapability.CLAUDE_SKILL,
        ),
        params=(
            PluginParam(
                name="text",
                type=PluginParamType.STRING,
                description="输入文本",
                required=True,
            ),
            PluginParam(
                name="keep_comments",
                type=PluginParamType.BOOL,
                description="保留注释",
                default=False,
            ),
        ),
        default_mounted=True,
    )


def _agent_manifest() -> PluginManifest:
    return PluginManifest(
        id="code_reviewer",
        name="Code Reviewer",
        version="0.1.0",
        category=PluginCategory.CODING_PLAN,
        description="自动代码审查",
        capabilities=(
            PluginCapability.MCP_TOOL,
            PluginCapability.SUBAGENT,
            PluginCapability.FILE_ACCESS,
        ),
        params=(
            PluginParam(
                name="path",
                type=PluginParamType.STRING,
                description="文件路径",
                required=True,
            ),
        ),
        sandbox_mode=SandboxMode.PROCESS,
    )


def _inference_manifest() -> PluginManifest:
    return PluginManifest(
        id="mlx_infer",
        name="MLX Inference",
        version="0.1.0",
        category=PluginCategory.MLX_INFERENCE,
        description="本地MLX推理",
        capabilities=(
            PluginCapability.MCP_TOOL,
            PluginCapability.VRAM_CONSUMER,
            PluginCapability.LONG_TASK,
        ),
        params=(),
        vram_mb=4096,
    )


# ── Flow 1: 注册 → MCP Tools 导出 → JSON-RPC 调用 ──


async def test_flow_register_to_mcp_jsonrpc() -> None:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    lifecycle = PluginLifecycle(registry)
    config = EcosystemConfig()
    handler = MCPHandler(
        registry=registry,
        lifecycle=lifecycle,
        desk=desk,
        config=config,
    )
    registry.register(_compressor_manifest())

    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
    )
    assert "tools" in resp["result"]
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "mcp__plugin__caveman_compress"

    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "ping",
        }
    )
    assert resp["result"] == {}


# ── Flow 2: 注册 → Skill/Agent 适配 → Bundle 生成 ──


def test_flow_register_to_bundle_generation() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    registry.register(_agent_manifest())
    registry.register(_inference_manifest())

    generator = PluginBundleGenerator(registry)

    bundle = generator.generate("caveman_compress")
    assert bundle is not None
    assert bundle.plugin_id == "caveman_compress"
    assert len(bundle.skills) > 0
    assert len(bundle.agents) == 0

    bundle = generator.generate("code_reviewer")
    assert bundle is not None
    assert len(bundle.skills) > 0
    assert len(bundle.agents) > 0

    bundle = generator.generate("mlx_infer")
    assert bundle is not None
    assert len(bundle.skills) > 0
    assert len(bundle.agents) == 0


# ── Flow 3: 注册 → ClaudeGateway 全链路 ──


def test_flow_claude_gateway_full() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    registry.register(_agent_manifest())

    config = EcosystemConfig(
        enable_claude_mcp=True,
        auto_export_claude_skill=True,
    )
    gateway = ClaudeGateway(registry=registry, config=config)

    skills = gateway.export_skills()
    assert len(skills) > 0

    mcp_tools = gateway.list_mcp_tools()
    assert len(mcp_tools) > 0

    mounted = gateway.export_default_mounted_skills()
    assert len(mounted) > 0
    mounted_ids = [s.get("name", s.get("id", "")) for s in mounted]
    assert any("caveman" in mid for mid in mounted_ids)


# ── Flow 4: 生命周期状态机 ──


async def test_flow_lifecycle_state_machine() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    lifecycle = PluginLifecycle(registry)

    # load → enable → disable → unload
    inst = lifecycle.load("caveman_compress")
    assert inst.state == PluginState.LOADED

    inst = await lifecycle.enable("caveman_compress")
    assert inst.state == PluginState.ENABLED

    await lifecycle.disable("caveman_compress")
    inst = lifecycle._instances["caveman_compress"]
    assert inst.state == PluginState.DISABLED

    lifecycle.unload("caveman_compress")
    assert "caveman_compress" not in lifecycle._instances


# ── Flow 5: Token 计量 + 持久化 ──


def test_flow_token_meter_persist_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "tokens.json")
        meter = TokenMeter(persist_path=path)
        meter.record(
            TokenRecord(
                plugin_id="caveman_compress",
                kind=TokenKind.CLAUDE_MODEL,
                input_tokens=100,
                output_tokens=50,
            )
        )
        meter.record(
            TokenRecord(
                plugin_id="code_reviewer",
                kind=TokenKind.PLUGIN_LOCAL,
                input_tokens=200,
                output_tokens=100,
            )
        )

        meter2 = TokenMeter(persist_path=path)
        records = meter2.records_for("caveman_compress")
        assert len(records) == 1
        assert records[0].input_tokens == 100

        records2 = meter2.records_for("code_reviewer")
        assert len(records2) == 1
        assert records2[0].output_tokens == 100


# ── Flow 6: MCP Exporter + Gateway 联动 ──


def test_flow_mcp_exporter_gateway_consistency() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    registry.register(_agent_manifest())
    registry.register(_inference_manifest())

    exporter = MCPExporter(registry)
    gateway = ClaudeGateway(registry=registry)

    exporter_tools = exporter.list_tools()
    gateway_tools = gateway.list_mcp_tools()
    assert len(exporter_tools) == len(gateway_tools)
    exporter_ids = {t["name"] for t in exporter_tools}
    gateway_ids = {t["name"] for t in gateway_tools}
    assert exporter_ids == gateway_ids


# ── Flow 7: Config 变更影响 Gateway 行为 ──


def test_flow_config_disables_mcp() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())

    config_on = EcosystemConfig(enable_claude_mcp=True)
    gateway_on = ClaudeGateway(registry=registry, config=config_on)
    assert len(gateway_on.list_mcp_tools()) > 0

    config_off = EcosystemConfig(enable_claude_mcp=False)
    gateway_off = ClaudeGateway(registry=registry, config=config_off)
    assert len(gateway_off.list_mcp_tools()) == 0


# ── Flow 8: SkillAdapter + AgentAdapter 独立导出 ──


def test_flow_adapters_export_all() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    registry.register(_agent_manifest())

    skill_adapter = SkillAdapter(registry)
    agent_adapter = AgentAdapter(registry)

    skill_bundles = skill_adapter.export_all()
    assert len(skill_bundles) == 1
    assert skill_bundles[0].skill_md.startswith("---\n")

    agent_mds = agent_adapter.export_all()
    assert len(agent_mds) == 1
    assert "code_reviewer" in agent_mds[0]


# ── Flow 9: 多插件注册 + 按分类过滤 ──


def test_flow_registry_category_filter() -> None:
    registry = PluginRegistry()
    registry.register(_compressor_manifest())
    registry.register(_agent_manifest())
    registry.register(_inference_manifest())

    compress = registry.list(category=PluginCategory.CONTEXT_COMPRESS)
    assert len(compress) == 1
    assert compress[0].id == "caveman_compress"

    inference = registry.list(category=PluginCategory.MLX_INFERENCE)
    assert len(inference) == 1
    assert inference[0].id == "mlx_infer"


# ── Flow 10: MCP JSON-RPC 错误处理 ──


async def test_flow_jsonrpc_method_not_found() -> None:
    desk = DeskRuntime()
    registry = PluginRegistry(desk=desk)
    lifecycle = PluginLifecycle(registry)
    config = EcosystemConfig()
    handler = MCPHandler(
        registry=registry,
        lifecycle=lifecycle,
        desk=desk,
        config=config,
    )
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "nonexistent/method",
            "params": {},
        }
    )
    assert "error" in resp
    assert resp["error"]["code"] == -32601


# ── Flow 11: Bundle 生成未知插件返回 None ──


def test_flow_bundle_unknown_plugin() -> None:
    registry = PluginRegistry()
    generator = PluginBundleGenerator(registry)
    assert generator.generate("nonexistent") is None


# ── Flow 12: Token 计量 prune + query ──


def test_flow_token_meter_prune_and_query() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "tokens.json")
        meter = TokenMeter(persist_path=path, max_records=5)
        for i in range(10):
            meter.record(
                TokenRecord(
                    plugin_id="p1",
                    kind=TokenKind.CLAUDE_MODEL,
                    input_tokens=i * 10,
                    output_tokens=i * 5,
                )
            )
        meter.prune()
        records = meter.records_for("p1")
        assert len(records) <= 5
