# fusion-plugins-ecosystem — plugin ecosystem layer built on fusion-cowork.
# Architecture: a submodule of the fusion-cowork runtime responsible for
# plugin registration, lifecycle, and full-chain Claude adaptation. All
# process hosting / permissions / hardware scheduling remain centrally
# provided by fusion-cowork.
# Lazy import mechanism (same as fusion-cowork):
# - importing fusion_plugins_ecosystem stays fast
# - the corresponding module loads on first attribute access

from __future__ import annotations

__version__ = "0.12.0"
__app_name__ = "Fusion-Plugins-Ecosystem"

# ── Lazy import registry ──
# Key: public attribute name; value: module path
_LAZY_IMPORTS: dict[str, str] = {
    # Registry
    "PluginRegistry": "fusion_plugins_ecosystem.registry",
    "PluginManifest": "fusion_plugins_ecosystem.registry",
    "PluginCapability": "fusion_plugins_ecosystem.registry",
    "PluginCategory": "fusion_plugins_ecosystem.registry",
    "PluginParam": "fusion_plugins_ecosystem.registry",
    # Tool manifest interchange (issue #68)
    "ToolManifest": "fusion_plugins_ecosystem.tool_manifest",
    "ToolApiSpec": "fusion_plugins_ecosystem.tool_manifest",
    "ToolManifestError": "fusion_plugins_ecosystem.tool_manifest",
    "parse_tool_manifest": "fusion_plugins_ecosystem.tool_manifest",
    "load_tool_manifest": "fusion_plugins_ecosystem.tool_manifest",
    "manifest_to_mcp": "fusion_plugins_ecosystem.tool_manifest",
    "manifest_from_mcp": "fusion_plugins_ecosystem.tool_manifest",
    # Lifecycle
    "PluginLifecycle": "fusion_plugins_ecosystem.lifecycle",
    "PluginState": "fusion_plugins_ecosystem.lifecycle",
    # Token metering
    "TokenMeter": "fusion_plugins_ecosystem.token_meter",
    "TokenRecord": "fusion_plugins_ecosystem.token_meter",
    "TokenKind": "fusion_plugins_ecosystem.token_meter",
    # Claude adaptation
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
    # Observability metrics
    "MetricsRegistry": "fusion_plugins_ecosystem.metrics",
    # Cluster distributed-state bridge (enterprise multi-node)
    "cluster_bridge": "fusion_plugins_ecosystem.cluster_bridge",
    # External MCP connector framework + marketplace
    "ConnectorRegistry": "fusion_plugins_ecosystem.connector",
    "ConnectorPack": "fusion_plugins_ecosystem.connector",
    "ConnectorToolSpec": "fusion_plugins_ecosystem.connector",
    "ExternalMCPClient": "fusion_plugins_ecosystem.connector",
    "RiskLevel": "fusion_plugins_ecosystem.connector",
    # Extensions console (issue #30 E3: declarative connectors + MCP intake
    # + plugin tick)
    "ExtensionsConsole": "fusion_plugins_ecosystem.extensions_console",
    "rest_connector": "fusion_plugins_ecosystem.rest_connector",
    # shell module registration (issue #30 acceptance follow-up:
    # fusion-gateway sidebar entry)
    "shell_registry": "fusion_plugins_ecosystem.shell_registry",
    "proctitle": "fusion_plugins_ecosystem.proctitle",
    "Marketplace": "fusion_plugins_ecosystem.marketplace",
    "MarketplaceEntry": "fusion_plugins_ecosystem.marketplace",
    # Config
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
    # Lazily load modules, importing only on first access.
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
                # The module itself is the export (e.g. cluster_bridge /
                # rest_connector)
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
    # List all accessible attributes.
    return sorted(set([*dir(type("", (), {})), *__all__, *_LAZY_IMPORTS.keys()]))


__all__ = [
    "__version__",
    "__app_name__",
    *_LAZY_IMPORTS.keys(),
]
