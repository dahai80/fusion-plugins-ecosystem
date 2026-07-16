# API Reference

> Public API of `fusion_plugins_ecosystem`. All symbols are exported via lazy import from the package root.

## Quick reference

```python
import fusion_plugins_ecosystem as fpe

# Registry
registry = fpe.PluginRegistry()
registry.register_builtin()           # loads caveman_compress

# Lifecycle
lifecycle = fpe.PluginLifecycle(registry)
await lifecycle.enable("caveman_compress")
result = await lifecycle.execute("caveman_compress", {"text": "# c\ncode"})

# Claude full-chain gateway
gw = fpe.ClaudeGateway(registry, lifecycle)
skills = gw.export_skills()           # → Claude Skill dicts
tools = gw.list_mcp_tools()           # → MCP tool descriptors
resp = await gw.invoke_mcp_tool("caveman_compress", {"text": "..."})

# Token metering
meter = fpe.TokenMeter()
with meter.measure("caveman", fpe.TokenKind.PLUGIN_LOCAL):
    ...

# Config
config = fpe.EcosystemConfig(enable_claude_mcp=False)
```

## `PluginRegistry` — registry.py

Declarative plugin manifest store. Holds manifests only; instantiation deferred to `PluginLifecycle`.

| Method | Signature | Notes |
|--------|-----------|-------|
| `__init__` | `(desk: DeskContext \| None = None)` | defaults to empty `DeskContext` |
| `register` | `(manifest: PluginManifest) -> None` | raises `ValueError` on duplicate id |
| `unregister` | `(plugin_id: str) -> None` | no-op if absent |
| `get` | `(plugin_id: str) -> PluginManifest \| None` | |
| `list` | `(category: PluginCategory \| None = None) -> list[PluginManifest]` | optional category filter |
| `register_builtin` | `() -> None` | lazy-loads `caveman_compress` |
| `default_mounted` | `() -> list[PluginManifest]` | plugins with `default_mounted=True` |

### `PluginManifest` (dataclass)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `id` | `str` | — | globally unique |
| `name` | `str` | — | user-friendly |
| `version` | `str` | — | semver-ish |
| `category` | `PluginCategory` | — | enum |
| `description` | `str` | — | one-liner, drives Claude Skill description |
| `capabilities` | `list[PluginCapability]` | `[]` | drives auto-exposure |
| `params` | `list[PluginParam]` | `[]` | JSON-schema-ish |
| `entry_point` | `Callable \| str \| None` | `None` | `(desk, params) -> result` or `module:attr` |
| `default_mounted` | `bool` | `False` | auto-mount to Claude session |
| `timeout_seconds` | `int \| None` | `None` | `None` inherits lifecycle default (600) |
| `vram_mb` | `int` | `0` | 0 = no vRAM allocation |

### `PluginParam` (dataclass)

| Field | Type | Default |
|-------|------|---------|
| `name` | `str` | — |
| `type` | `str` | — (`string`/`int`/`bool`/`array`/`object`/`float`) |
| `description` | `str` | — |
| `required` | `bool` | `False` |
| `default` | `Any` | `None` |
| `enum` | `list[str] \| None` | `None` |

### `PluginCapability` (enum)

`MCP_TOOL`, `CLAUDE_SKILL`, `SUBAGENT`, `FILE_ACCESS`, `VRAM_CONSUMER`, `LONG_TASK`.

### `PluginCategory` (enum)

`CODING_PLAN`, `CONTEXT_COMPRESS`, `MLX_INFERENCE`, `TERMINAL_PROXY`, `FILE_INDEX`, `QUANTIZATION`, `VISUAL_BACKEND`, `CUSTOM`.

## `PluginLifecycle` — lifecycle.py

| Method | Signature | Notes |
|--------|-----------|-------|
| `load` | `(plugin_id) -> PluginInstance` | resolves `entry_point` (callable kept as-is, class instantiated, `module:attr` string imported) |
| `enable` | `async (plugin_id) -> PluginInstance` | acquires vRAM; sets `ENABLED`; on vRAM failure → `CRASHED` |
| `disable` | `async (plugin_id) -> None` | releases vRAM; sets `DISABLED` |
| `unload` | `(plugin_id) -> None` | releases vRAM, drops instance, keeps manifest |
| `execute` | `async (plugin_id, params) -> Any` | requires `ENABLED`; timeout meltdown on `timeout_seconds`; auto-restart ≤ `MAX_RESTART` |
| `start_watcher` | `async () -> None` | idempotent; spawns heartbeat stall detector |
| `stop_watcher` | `async () -> None` | cancels watcher |

Class constants: `DEFAULT_TIMEOUT=600`, `MAX_RESTART=3`, `HEARTBEAT_STALE=120`.

### `PluginState` (enum)

`REGISTERED`, `LOADED`, `ENABLED`, `DISABLED`, `CRASHED`, `TIMEOUT`.

## `ClaudeGateway` — claude_gateway.py

Unified Claude full-chain entry. Thin orchestrator delegating to `ClaudeSkillAdapter`, `MCPExporter`, `PluginLifecycle`, `DeskContext`, `TokenMeter`.

| Method | Direction | Signature | Notes |
|--------|-----------|-----------|-------|
| `export_skills` | forward | `() -> list[dict]` | gated by `config.auto_export_claude_skill` |
| `export_default_mounted_skills` | forward | `() -> list[dict]` | gated by `config.default_mount_compressor` |
| `list_mcp_tools` | forward | `() -> list[dict]` | gated by `config.enable_claude_mcp` |
| `invoke_mcp_tool` | forward | `async (plugin_id, args) -> dict` | MCP tools/call response `{content, isError}` |
| `gateway_info` | forward | `() -> dict` | transport/port/protocol_version/skills_count/config |
| `dispatch_subagent` | reverse | `async (task: SubagentTask) -> dict` | `{task, plugin_id, state, result \| error}` |
| `list_subagent_capable_plugins` | reverse | `() -> list[str]` | plugins with `SUBAGENT` capability |
| `store_credentials` | auth | `(provider, api_key) -> None` | volcengine gated by `enable_volcengine_claude_plan` |
| `get_credentials` | auth | `(provider) -> str \| None` | |
| `has_credentials` | auth | `(provider) -> bool` | |
| `mlx_visual_backend` | backend | `async (model, messages, **kw) -> Any` | gated by `enable_mixed_quantization` |

### `SubagentTask` (dataclass)

| Field | Type | Default |
|-------|------|---------|
| `name` | `str` | — |
| `plugin_id` | `str` | — |
| `arguments` | `dict[str, Any]` | — |
| `timeout_seconds` | `int \| None` | `None` (inherits config) |
| `metadata` | `dict[str, Any]` | `{}` |

### Access constants

`CLAUDE_DESKTOP="claude_desktop"`, `CLAUDE_CODE="claude_code"`, `CLAUDE_WEB="claude_web"`, `CLAUDE_VOLCENGINE="claude_volcengine"`.

## `ClaudeSkillAdapter` — claude_adapter.py

| Method | Signature | Notes |
|--------|-----------|-------|
| `export_one` | `(plugin_id) -> dict \| None` | Claude Skill dict |
| `export_all` | `() -> list[dict]` | only `CLAUDE_SKILL`-capable plugins |
| `export_default_mounted` | `() -> list[dict]` | `default_mounted=True` only |

Skill dict shape: `{name, description, input_schema: {type, properties, required?}, _fusion: {plugin_name, version, category, capabilities, default_mounted, timeout_seconds}}`.

## `MCPExporter` — mcp_exporter.py

| Method | Signature | Notes |
|--------|-----------|-------|
| `list_tools` | `() -> list[dict]` | only `MCP_TOOL`-capable plugins |
| `call_tool` | `async (plugin_id, args) -> dict` | logs relay, returns MCP response |
| `gateway_info` | `() -> dict` | `{transport, port, tools_count, protocol_version}` |

Tool dict shape: `{name, description, inputSchema: {type, properties, required?}}`.

## `TokenMeter` — token_meter.py

| Method | Signature | Notes |
|--------|-----------|-------|
| `record` | `(rec: TokenRecord) -> None` | warns on `PLUGIN_LOCAL ∧ total=0 ∧ wall>60` |
| `measure` | `(plugin_id, kind, input_tokens=0, output_tokens=0, metadata=None) -> ctx` | context manager, auto-records `wall_seconds` |
| `summary` | `() -> dict[str, dict[str, int]]` | `{plugin_id: {kind: total_tokens}}` |
| `records_for` | `(plugin_id) -> list[TokenRecord]` | |
| `all_records` | `() -> list[TokenRecord]` | insertion order |

### `TokenKind` (enum)

`CLAUDE_MODEL`, `PLUGIN_LOCAL`, `MLX_INFERENCE`, `MCP_RELAY`.

### `TokenRecord` (dataclass)

`plugin_id`, `kind`, `input_tokens=0`, `output_tokens=0`, `total_tokens` (=sum if 0), `wall_seconds=0.0`, `timestamp`, `metadata={}`.

## `DeskContext` / `DeskRuntime` — desk_context.py / desk_runtime.py

`DeskContext` is a thin bridge; `DeskRuntime` holds the real handles.

| Method | Notes |
|--------|-------|
| `acquire_vram(plugin_id, mb) -> bool` | enforces `vram_total_mb` budget |
| `release_vram(plugin_id) -> None` | |
| `vram_usage() -> dict[str, int]` | |
| `log(plugin_id, level, message, **kw) -> None` | routes to `desk_logger` or falls back to module logger |
| `check_file_permission(plugin_id, path) -> bool` | empty allowlist = allow all |
| `grant_permission(plugin_id, allowed_paths) -> None` | |
| `get_api_key(provider) -> str \| None` | via `config_center.get(f"api_keys.{provider}")` |
| `set_api_key(provider, key) -> None` | |
| `mlx_chat / mlx_embed / mlx_health` | delegates to `mlx_client` |
| `list_nodes / resolve_node` | delegates to `node_registry` |
| `list_scheduled_tasks` | delegates to `task_scheduler` |
| `gateway_info() -> dict` | `{transport, port, protocol_version}` |

## `EcosystemConfig` — config.py

Dataclass, all defaults `True` (native full Claude compatibility). Persistable via `to_dict` / `from_dict`.

| Field | Default | Controls |
|-------|---------|----------|
| `enable_claude_mcp` | `True` | `list_mcp_tools`, `invoke_mcp_tool` |
| `auto_export_claude_skill` | `True` | `export_skills` |
| `subagent_timeout_destroy` | `True` | `dispatch_subagent` unload on timeout |
| `default_mount_compressor` | `True` | `export_default_mounted_skills` |
| `enable_volcengine_claude_plan` | `True` | volcengine credential store/get |
| `unified_log_to_desk` | `True` | (informational; enforced by `DeskContext.log`) |
| `enable_mixed_quantization` | `True` | `mlx_visual_backend` |
| `subagent_timeout_seconds` | `600` | `dispatch_subagent` default timeout |
| `heartbeat_stale_seconds` | `120` | watcher threshold |
| `max_auto_restart` | `3` | lifecycle restart cap |

## `caveman_compress` — builtin/caveman_compress.py

Built-in token compressor, `default_mounted=True`.

```python
from fusion_plugins_ecosystem.builtin.caveman_compress import (
    CAVEMAN_MANIFEST, caveman_compress,
)
result = caveman_compress(desk, {"text": "# comment\ncode", "keep_comments": False})
# → {"compressed": "code", "original_chars": 13, "compressed_chars": 4, "ratio": 0.31, "strategy": "caveman"}
```

Params: `text` (str, required), `keep_comments` (bool, default `False`), `strategy` (str, enum `["caveman"]`).
