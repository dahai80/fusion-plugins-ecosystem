---
doc_id: fusion-plugins-ecosystem/api-reference
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: api-reference
title: {zh: "API 参考", en: "API Reference"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - fusion_plugins_ecosystem/registry.py
  - fusion_plugins_ecosystem/claude_gateway.py
  - fusion_plugins_ecosystem/desk_runtime.py
  - fusion_plugins_ecosystem/__init__.py
---

# API 参考 / API Reference

签名均摘自源码（v0.8.0）。顶层经 `fusion_plugins_ecosystem as fpe` 惰性导入暴露。

Signatures are taken from source (v0.8.0). Top-level names are exposed lazily via `fusion_plugins_ecosystem as fpe`.

## PluginRegistry（registry.py）

- `PluginRegistry(desk: DeskRuntime | None = None)`
- `register(manifest: PluginManifest)` — 冻结 manifest，id 全局唯一 / frozen manifest, globally unique id
- `register_builtin()` — 注册内置 `caveman_compress`（幂等）/ registers the built-in caveman compressor (idempotent)
- `get(plugin_id)` / `list_plugins()` / `unregister(plugin_id)` — 稳定读契约 registry/1.0（issue #44）/ stable read contract

### PluginManifest（frozen dataclass）

| 字段 / Field | 类型 / Type | 默认 / Default | 说明 / Notes |
|---|---|---|---|
| id | str | 必填 / required | 全局唯一，如 `caveman_compress` / globally unique |
| name / version / description | str | 必填 / required | |
| category | PluginCategory | 必填 / required | 见下 / see below |
| capabilities | tuple[PluginCapability, ...] | `()` | 驱动自动暴露 / drives auto-exposure |
| params | tuple[PluginParam, ...] | `()` | 参数 schema / param schema |
| entry_point | Callable \| str \| None | None | 惰性导入支持字符串 / string = lazy import |
| timeout_seconds | int \| None | None | |
| default_mounted | bool | False | Claude 会话默认挂载 / mounted by default |
| vram_mb | int | 0 | 需 Desk VRAM 调度 / Desk VRAM scheduling |
| depends_on | tuple[str, ...] | `()` | 拓扑序加载 / topological load order |
| sandbox_mode | SandboxMode | `INLINE` | `inline` / `process` |
| max_restart | int \| None | None | None = 继承全局 / inherit global |
| output_schema | dict \| None | None | MCP 2026-07-28 outputSchema |
| mcp_annotations | MCPAnnotations \| None | None | MCP 工具注解 / tool annotations |
| agent_model | str \| None | None | 子代理模型 / subagent model |

### PluginCapability

`MCP_TOOL` (`mcp_tool`)、`CLAUDE_SKILL` (`claude_skill`)、`SUBAGENT` (`subagent`)、`FILE_ACCESS` (`file_access`)、`VRAM_CONSUMER` (`vram_consumer`)、`LONG_TASK` (`long_task`)

### PluginCategory

`coding_plan`、`context_compress`、`mlx_inference`、`terminal_proxy`、`file_index`、`quantization`、`visual_backend`、`connector`、`custom`

### PluginParam（frozen dataclass）

`name: str`、`type: "string"|"int"|"bool"|"float"|"array"|"object"`、`description: str`、`required: bool = False`、`default: Any = None`、`enum: tuple[str, ...] | None`

## PluginLifecycle（lifecycle.py）

- `PluginLifecycle(registry, config: EcosystemConfig | None = None)`
- `async execute(plugin_id, arguments)` — 唯一执行入口：load→enable→run，超时熔断、自动重启、并发闸（`max_concurrent_plugins`，默认 16）/ single execution path with meltdown, restart, concurrency gate
- enable/disable/load/unload、热重载 / hot reload

## ClaudeGateway（claude_gateway.py）

- `ClaudeGateway(registry, lifecycle, ...)`
- `export_skills() -> list[dict]` / `export_default_mounted_skills() -> list[dict]`
- `list_mcp_tools() -> list[dict]` — MCP 工具描述 / MCP tool descriptors
- `async invoke_mcp_tool(name, arguments)` — 正向调用 / forward call
- `gateway_info() -> dict`
- `async dispatch_subagent(task: SubagentTask) -> dict` — 反向子代理，返回 `{"state", "result"}` / reverse subagent
- `list_subagent_capable_plugins() -> list[str]`
- `store_credentials(provider, api_key)` / `get_credentials(provider)` / `has_credentials(provider)` — Volcengine Coding Plan 鉴权等 / Volcengine Coding Plan auth
- `async mlx_visual_backend(...)` — fusion-mlx 作为视觉/图像生成后端 / fusion-mlx as visual backend

## DeskRuntime（desk_runtime.py）— 插件内 `desk` 参数

- `log(plugin_id, level, message, **fields)` — 统一日志汇总到 Desk / unified logging
- `acquire_vram(plugin_id, mb) -> bool` / `release_vram(plugin_id)` / `vram_usage()`
- `check_file_permission(plugin_id, path) -> bool` / `grant_permission(plugin_id, allowed_paths)`
- `get_api_key(provider) -> str | None` / `set_api_key(provider, key)` — Fernet 加密存储 / Fernet-encrypted
- `async mlx_chat(model, messages, **kwargs)` / `async mlx_embed(text, model="BGE-M3")` / `async mlx_health() -> bool`
- 集群 / cluster：`cluster_heartbeat(...)`、`is_plugin_enabled_anywhere(plugin_id)`、`plugin_state_across_cluster(plugin_id)`

## ConnectorRegistry（connector.py）与 Marketplace（marketplace.py）

- `ConnectorRegistry(desk, registry, persist_path=None)` — `install_pack(pack)`、`approve_tool(pack_id, tool)`、工具桥接为 `connector__<pack>__<tool>`
- `Marketplace(creg, marketplace_index=None)` — `load_index() -> (entries, warnings)`（内置目录 + 远程索引，索引按 id 覆盖内置）、`install(pid)`、`list_installed()`
- 连接器包为纯数据；索引支持本地 JSON 路径或 HTTPS URL（HTTPS-only 约束，测试强制）/ packs are pure data; index accepts a local JSON path or HTTPS URL (HTTPS-only, test-enforced)

## EcosystemConfig（config.py）

一键开关面板，全字段见 [config-reference.md](config-reference.md)；持久化到 Desk 配置中心，支持 observer 回调。 / One-toggle config panel; all fields in [config-reference.md](config-reference.md); persisted to the Desk config center with observer callbacks.

## CLI

| 命令 / Command | 参数 / Args |
|---|---|
| `fusion-plugin-server` | `--transport {stdio,sse,http}`（默认 stdio）、`--host`（默认 127.0.0.1）、`--port`（默认 `service_port("plugins-ecosystem", 0)`，0=auto；env `FUSION_PLUGINS_ECOSYSTEM_PORT`）、`--log-level {DEBUG,INFO,WARNING,ERROR}` |
| `fusion-plugin-console` | `--host`（默认 127.0.0.1）、`--port`（默认 8795；env `FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT`）、`--log-level` |
