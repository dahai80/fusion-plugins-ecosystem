"""fusion-plugins-ecosystem — 基于 fusion-cowork 的插件生态上层模块。

架构定位：fusion-cowork runtime 的子模块，负责插件注册、生命周期、
Claude 全链路适配。所有进程托管/权限/硬件调度仍由 fusion-cowork 统一提供。

Lazy Import 机制（与 fusion-cowork 一致）：
- import fusion_plugins_ecosystem 保持快速
- 首次访问属性时自动加载对应模块
"""

from __future__ import annotations

__version__ = "0.4.5"
__app_name__ = "Fusion-Plugins-Ecosystem"

# ── Lazy Import 注册表 ──
# 键：公开属性名，值：模块路径
_LAZY_IMPORTS: dict[str, str] = {
    # 注册中心
    "PluginRegistry": "fusion_plugins_ecosystem.registry",
    "PluginManifest": "fusion_plugins_ecosystem.registry",
    "PluginCapability": "fusion_plugins_ecosystem.registry",
    "PluginCategory": "fusion_plugins_ecosystem.registry",
    "PluginParam": "fusion_plugins_ecosystem.registry",
    # 生命周期
    "PluginLifecycle": "fusion_plugins_ecosystem.lifecycle",
    "PluginState": "fusion_plugins_ecosystem.lifecycle",
    # Token 计量
    "TokenMeter": "fusion_plugins_ecosystem.token_meter",
    "TokenRecord": "fusion_plugins_ecosystem.token_meter",
    "TokenKind": "fusion_plugins_ecosystem.token_meter",
    # Claude 适配
    "ClaudeSkillAdapter": "fusion_plugins_ecosystem.claude_adapter",
    "SkillAdapter": "fusion_plugins_ecosystem.skill_adapter",
    "SkillBundle": "fusion_plugins_ecosystem.skill_adapter",
    "AgentAdapter": "fusion_plugins_ecosystem.agent_adapter",
    "PluginBundle": "fusion_plugins_ecosystem.plugin_bundle",
    "PluginBundleGenerator": "fusion_plugins_ecosystem.plugin_bundle",
    "HookAdapter": "fusion_plugins_ecosystem.hook_adapter",
    "HookDef": "fusion_plugins_ecosystem.hook_adapter",
    "HookEvent": "fusion_plugins_ecosystem.hook_adapter",
    "MCPExporter": "fusion_plugins_ecosystem.mcp_exporter",
    "ClaudeGateway": "fusion_plugins_ecosystem.claude_gateway",
    "SubagentTask": "fusion_plugins_ecosystem.claude_gateway",
    "CLAUDE_DESKTOP": "fusion_plugins_ecosystem.claude_gateway",
    "CLAUDE_CODE": "fusion_plugins_ecosystem.claude_gateway",
    "CLAUDE_WEB": "fusion_plugins_ecosystem.claude_gateway",
    "CLAUDE_VOLCENGINE": "fusion_plugins_ecosystem.claude_gateway",
    # Desk runtime
    "DeskRuntime": "fusion_plugins_ecosystem.desk_runtime",
    # 可观测性指标
    "MetricsRegistry": "fusion_plugins_ecosystem.metrics",
    # 集群分布式状态桥接（企业级多节点）
    "cluster_bridge": "fusion_plugins_ecosystem.cluster_bridge",
    # 外部 MCP 连接器框架 + 市场
    "ConnectorRegistry": "fusion_plugins_ecosystem.connector",
    "ConnectorPack": "fusion_plugins_ecosystem.connector",
    "ConnectorToolSpec": "fusion_plugins_ecosystem.connector",
    "ExternalMCPClient": "fusion_plugins_ecosystem.connector",
    "RiskLevel": "fusion_plugins_ecosystem.connector",
    # Extensions 控制台（issue #30 E3：声明式连接器 + MCP intake + 插件 tick）
    "ExtensionsConsole": "fusion_plugins_ecosystem.extensions_console",
    "rest_connector": "fusion_plugins_ecosystem.rest_connector",
    # shell 模块注册（issue #30 验收补充：fusion-gateway 侧边栏入口）
    "shell_registry": "fusion_plugins_ecosystem.shell_registry",
    "Marketplace": "fusion_plugins_ecosystem.marketplace",
    "MarketplaceEntry": "fusion_plugins_ecosystem.marketplace",
    # 配置
    "EcosystemConfig": "fusion_plugins_ecosystem.config",
    # Schema
    "PluginParamType": "fusion_plugins_ecosystem.schema",
    "SandboxMode": "fusion_plugins_ecosystem.schema",
    "MCPAnnotations": "fusion_plugins_ecosystem.schema",
    "AsyncMeasureContext": "fusion_plugins_ecosystem.token_meter",
    # MCP Server
    "MCPServer": "fusion_plugins_ecosystem.server",
    "MCPHandler": "fusion_plugins_ecosystem.jsonrpc",
    "StdioTransport": "fusion_plugins_ecosystem.transport",
    "SSETransport": "fusion_plugins_ecosystem.transport",
    "HTTPTransport": "fusion_plugins_ecosystem.transport",
    "create_transport": "fusion_plugins_ecosystem.transport",
    # Sandbox
    "PluginSandbox": "fusion_plugins_ecosystem.sandbox",
    "SandboxHealth": "fusion_plugins_ecosystem.sandbox",
    "ResourceLimits": "fusion_plugins_ecosystem.sandbox",
}

_lazy_cache: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """延迟加载模块，仅在首次访问时导入。"""
    if name in _lazy_cache:
        return _lazy_cache[name]
    if name in _LAZY_IMPORTS:
        import importlib

        mod_name = _LAZY_IMPORTS[name]
        try:
            mod = importlib.import_module(mod_name)
            try:
                obj = getattr(mod, name)
            except AttributeError:
                # 模块自身导出（如 cluster_bridge / rest_connector）
                if mod_name.endswith("." + name):
                    _lazy_cache[name] = mod
                    return mod
                raise
            _lazy_cache[name] = obj
            return obj
        except (ImportError, AttributeError) as exc:
            raise AttributeError(
                f"module 'fusion_plugins_ecosystem' has no attribute {name!r} "
                f"(lazy import {mod_name!r} failed: {exc})"
            ) from None
    raise AttributeError(f"module 'fusion_plugins_ecosystem' has no attribute {name!r}")


def __dir__() -> list[str]:
    """列出所有可访问的属性。"""
    return sorted(set([*dir(type("", (), {})), *__all__, *_LAZY_IMPORTS.keys()]))


__all__ = [
    "__version__",
    "__app_name__",
    *_LAZY_IMPORTS.keys(),
]
