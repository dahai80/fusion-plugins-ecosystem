---
doc_id: docs/plugin-development
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: guide
title: {zh: "插件开发指南", en: "Plugin Development Guide"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/PLUGIN_DEVELOPMENT.md
  - fusion_plugins_ecosystem/registry.py
---

# Plugin Development Guide (English)

> Write a plugin once → it auto-appears as a Claude Code Skill, an MCP Tool (2026-07-28), and (optionally) a Claude Code subagent. Zero adapter code.

This guide grounds every statement in the real API of `fusion-plugins-ecosystem` v0.3.3. For the Chinese mirror see [PLUGIN_DEVELOPMENT_CN.md](PLUGIN_DEVELOPMENT_CN.md). For runnable scenarios see [`../examples/`](../examples/).

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [The 30-Second Plugin](#2-the-30-second-plugin)
3. [PluginManifest — every field](#3-pluginmanifest--every-field)
4. [PluginParam — typed inputs](#4-pluginparam--typed-inputs)
5. [Capabilities → what auto-exposes](#5-capabilities--what-auto-exposes)
6. [Entry Point Contract](#6-entry-point-contract)
7. [The `desk` argument — DeskRuntime API](#7-the-desk-argument--deskruntime-api)
8. [Lifecycle: load/enable/execute](#8-lifecycle-loadenableexecute)
9. [Async, Timeouts, and Long Tasks](#9-async-timeouts-and-long-tasks)
10. [Sandbox Modes: INLINE vs PROCESS](#10-sandbox-modes-inline-vs-process)
11. [Categories](#11-categories)
12. [Registering & Running](#12-registering--running)
13. [Reverse Path: Subagent Dispatch](#13-reverse-path-subagent-dispatch)
14. [MCP Annotations & Output Schema](#14-mcp-annotations--output-schema)
15. [Testing Checklist](#15-testing-checklist)
16. [Examples Index](#16-examples-index)

---

## 1. Mental Model

A plugin is **one callable** + **one manifest**.

```
your_plugin(desk, params) -> dict     ← the callable (sync or async)
MY_MANIFEST = PluginManifest(...)      ← the declaration
```

- The **manifest** is declarative, immutable (`@dataclass(frozen=True)`), and never holds a running instance. It drives Claude Skill generation, MCP Tool definition, and lifecycle behavior.
- The **callable** is `(desk: DeskRuntime, params: dict) -> result`. `desk` is your only handle to fusion-cowork runtime (logs, vRAM, MLX, node registry). `params` is the validated input dict.
- Registering the manifest is the only thing you do. Lifecycle (load/enable/execute), MCP wire, and Claude adaptation happen automatically.

You **never** import `fusion_cowork` internals directly — everything goes through `desk`.

## 2. The 30-Second Plugin

```python
from fusion_plugins_ecosystem.registry import (
    PluginCapability, PluginCategory, PluginManifest, PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType
import logging

logger = logging.getLogger(__name__)


def my_echo(desk, params):
    text = params["text"]
    if desk is not None:
        desk.log("my_echo", "INFO", "echoed", chars=len(text))
    return {"echo": text.upper()}


MY_ECHO_MANIFEST = PluginManifest(
    id="my_echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Echoes input text uppercased.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL),
    params=(
        PluginParam(name="text", type=PluginParamType.STRING,
                    description="Input text", required=True),
    ),
    entry_point=my_echo,
    timeout_seconds=60,
)
```

Register and call:

```python
import fusion_plugins_ecosystem as fpe

registry = fpe.PluginRegistry()
registry.register(MY_ECHO_MANIFEST)
lifecycle = fpe.PluginLifecycle(registry)
await lifecycle.enable("my_echo")           # load + enable
result = await lifecycle.execute("my_echo", {"text": "hello"})
# → {"echo": "HELLO"}
```

That manifest alone also yields:
- `ClaudeGateway(registry, lifecycle).list_mcp_tools()` → an MCP tool named `mcp__plugin__my_echo`
- `ClaudeGateway(...).export_skills()` → a Claude Code Skill bundle

## 3. PluginManifest — every field

`PluginManifest` (`registry.py`) is a frozen dataclass. Field reference:

| Field | Type | Default | Purpose |
|---|---|---|---|
| `id` | `str` | *(required)* | Globally unique plugin id, e.g. `"my_echo"`. Becomes the MCP tool suffix. |
| `name` | `str` | *(required)* | Human-friendly display name. |
| `version` | `str` | *(required)* | Semver-ish version. Re-registering the same id with a different version raises `ValueError`. |
| `category` | `PluginCategory` | *(required)* | One of the 8 categories (see §11). |
| `description` | `str` | *(required)* | One-liner shown in tool lists and Skill descriptions. |
| `capabilities` | `tuple[PluginCapability,...]` | `()` | What auto-exposes (see §5). |
| `params` | `tuple[PluginParam,...]` | `()` | Typed input schema (see §4). |
| `entry_point` | `Callable \| str \| None` | `None` | The callable, or `"module:attr"` dotted path (required for PROCESS sandbox, see §6/§10). |
| `default_mounted` | `bool` | `False` | If `True`, auto load+enable when `config.default_mount_compressor` is on (used by built-in `caveman_compress`). |
| `timeout_seconds` | `int \| None` | `None` | Per-call timeout. `None` → inherits lifecycle `DEFAULT_TIMEOUT=600`. |
| `vram_mb` | `int` | `0` | Estimated VRAM footprint in MB. `0` = none. |
| `depends_on` | `tuple[str,...]` | `()` | Other plugin ids loaded first (topological order via `registry.resolve_load_order()`). |
| `sandbox_mode` | `SandboxMode` | `INLINE` | `INLINE` (in-process) or `PROCESS` (subprocess isolation). See §10. |
| `max_restart` | `int \| None` | `None` | Crash auto-restart cap. `None` → global `MAX_RESTART=3`. |
| `output_schema` | `dict \| None` | `None` | MCP 2026-07-28 `outputSchema` for the tool result. |
| `mcp_annotations` | `MCPAnnotations \| None` | `None` | MCP tool behavior hints (readOnly/destructive/idempotent/openWorld). See §14. |
| `agent_model` | `str \| None` | `None` | Claude model hint for subagent dispatch (reverse path, §13). |

## 4. PluginParam — typed inputs

Each `PluginParam` declares one input field. It feeds both the Claude Skill parameter description and the MCP Tool JSON Schema.

```python
PluginParam(
    name="max_tokens",
    type=PluginParamType.INT,
    description="Maximum tokens to generate",
    required=False,
    default=256,
    enum=None,
)
```

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Key inside `params` dict. |
| `type` | `PluginParamType \| str` | `STRING`/`INT`/`BOOL`/`FLOAT`/`ARRAY`/`OBJECT`. Plain strings (`"string"`) still accepted for backward compat and mapped via `PARAM_TYPE_TO_JSON_SCHEMA`. |
| `description` | `str` | Human description. |
| `required` | `bool` | Default `False`. |
| `default` | `Any` | Default value when omitted. |
| `enum` | `tuple[str,...] \| None` | Constrains to a fixed set (e.g. `("caveman",)`). |

In your entry point, read with `params.get(name, default)`:

```python
text = params.get("text", "")
keep = params.get("keep_comments", False) is True
```

## 5. Capabilities → what auto-exposes

`PluginCapability` (in `registry.py`) drives automatic exposure. Add capabilities to the tuple; each one has an effect:

| Capability | Effect |
|---|---|
| `MCP_TOOL` | Listed in `MCPExporter.list_tools` / `ClaudeGateway.list_mcp_tools` as `mcp__plugin__<id>`, callable via `tools/call`. |
| `CLAUDE_SKILL` | Exported by `SkillAdapter` (and legacy `ClaudeSkillAdapter`) as a Claude Code Skill bundle. |
| `SUBAGENT` | Listed in `ClaudeGateway.list_subagent_capable_plugins`; usable as the `plugin_id` of a `SubagentTask` (reverse dispatch, §13). |
| `VRAM_CONSUMER` | VRAM acquired/released via `DeskRuntime` on enable/disable. **INLINE only** — PROCESS sandbox raises `NotImplementedError` for VRAM. |
| `LONG_TASK` | Subject to timeout meltdown + auto-restart (≤ `max_restart` / global `MAX_RESTART=3`). |
| `FILE_ACCESS` | File permission checked via `DeskRuntime.check_file_permission`. |

A plugin can have several: `caveman_compress` declares `(MCP_TOOL, CLAUDE_SKILL)`.

## 6. Entry Point Contract

Every plugin entry point has signature `(desk: DeskRuntime, params: dict) -> result`.

Four forms are accepted:

1. **A callable** (function or class instance) — used directly. Inline-mode default.
2. **A class** — instantiated on load (`lifecycle` calls `entry_point()` with no args to build the instance, then `(instance)(desk, params)`).
3. **A `"module:attr"` string** — resolved via `importlib` on first load. **Required for PROCESS sandbox** (the subprocess must be able to import it).
4. **An async callable** — auto-detected by `inspect.iscoroutinefunction(instance)` in `PluginLifecycle._invoke_inline` (`lifecycle.py:299`); awaited automatically.

**Return value** is formatted into MCP by `_format_result` (`jsonrpc.py`):
- `dict` with a `"content"` key that is a `list` → used verbatim as the MCP content array (full control).
- `dict` without `"content"` → JSON-serialized into a single `{"type":"text","text":<json>}` content block.
- `str` → `{"type":"text","text":<str>}`.
- `list` → used as-is (must be a valid MCP content array).
- anything else → JSON-serialized text block.

So the simplest return is a plain `dict` of your result fields.

**PROCESS sandbox constraint**: the entry point **must be a module-level attribute** — closures, lambdas, and locally-defined functions (anything with `<locals>` in `__qualname__`) cannot be imported by the subprocess and will fail at spawn. Use a module-level function/class or a `"module:attr"` string.

## 7. The `desk` argument — DeskRuntime API

`desk` is your only bridge to fusion-cowork. Call its methods; never import `fusion_cowork` directly. Any field may be `None` in degraded/offline mode — guard accordingly. The module flag `FUSION_COWORK_AVAILABLE` indicates whether `fusion_cowork` was importable.

### DeskRuntime method reference (verbatim signatures)

| Method | Signature | Purpose |
|---|---|---|
| `log` | `log(plugin_id: str, level: str, message: str, **kwargs) -> None` | Unified log ingestion — fans out to the logger and appends to the log ring buffer. `level` is a string name (`"INFO"`/`"WARN"`/`"ERROR"`/`"DEBUG"`). |
| `get_logs` | `get_logs(plugin_id: str \| None = None, level: str \| None = None, limit: int = 200) -> list[dict]` | Query log buffer with optional filters; returns most-recent `limit` entries. |
| `acquire_vram` | `acquire_vram(plugin_id: str, mb: int) -> bool` | Request VRAM. **Resize semantics** (replaces prior allocation for `plugin_id`, not additive). Returns `True` on success, `False` if over budget. `mb <= 0` releases. |
| `release_vram` | `release_vram(plugin_id: str) -> None` | Release VRAM held by `plugin_id`. |
| `vram_usage` | `vram_usage() -> dict[str, int]` | Snapshot of current VRAM ledger `{plugin_id: mb}`. |
| `check_file_permission` | `check_file_permission(plugin_id: str, path: str) -> bool` | Check path access. Empty `allowed_paths` allowlist = allow all. |
| `grant_permission` | `grant_permission(plugin_id: str, allowed_paths: list[str]) -> None` | Grant a path-access allowlist to a plugin. |
| `get_api_key` | `get_api_key(provider: str) -> str \| None` | Read API key from the config center at `api_keys.{provider}`. `None` if missing. |
| `set_api_key` | `set_api_key(provider: str, key: str) -> None` | Write an API key. |
| `mlx_chat` | `async mlx_chat(model: str, messages: list[dict], **kwargs) -> Any` | Call fusion-mlx local inference. Raises `RuntimeError` if `mlx_client is None`. |
| `mlx_embed` | `async mlx_embed(text: str, model: str = "BGE-M3") -> Any` | Generate a text embedding via fusion-mlx. Raises if `mlx_client is None`. |
| `mlx_health` | `async mlx_health() -> bool` | fusion-mlx health check. Returns `False` if `mlx_client is None`. |
| `list_nodes` | `list_nodes() -> list[dict]` | List registered fusion-cowork node types. `[]` if no registry. |
| `resolve_node` | `resolve_node(name: str) -> Any \| None` | Resolve a node name (alias-aware) to its class. `None` if absent. |
| `list_scheduled_tasks` | `list_scheduled_tasks() -> list[Any]` | List fusion-cowork scheduled tasks. `[]` if no scheduler. |
| `gateway_info` | `gateway_info() -> dict` | MCP gateway info `{"transport":"stdio","port":<mcp_gateway_port>,"protocol_version":"2026-07-28"}`. |

### Logging

All plugin logs go through `desk.log(plugin_id, level, message, **fields)` — **never** `print()` or module-level handlers that bypass the panel. Extra fields become structured log context:

```python
desk.log("my_echo", "INFO", "echoed", chars=len(text), ratio=0.7)
```

### VRAM

`VRAM_CONSUMER` plugins acquire/release VRAM through `desk` automatically on enable/disable (lifecycle wires `acquire_vram`/`release_vram` from `manifest.vram_mb`). Declare the footprint with `vram_mb`. Note `acquire_vram` is **resize-not-additive**: calling it again replaces, not stacks.

### Credentials

`get_api_key`/`set_api_key` back the Volcengine Claude plan and any provider keys. Store at `api_keys.{provider}`.

## 8. Lifecycle: load/enable/execute

`PluginLifecycle` (`lifecycle.py`) manages instances. Class constants:

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_TIMEOUT` | `600` | Fallback per-call timeout (seconds). |
| `MAX_RESTART` | `3` | Fallback crash auto-restart cap. |
| `HEARTBEAT_STALE` | `120` | Seconds without heartbeat → judged stuck. |

Public methods (verbatim signatures):

| Method | Signature | Purpose |
|---|---|---|
| `load` | `load(plugin_id: str) -> PluginInstance` | Load: resolve entry point (import `"mod:attr"` or use callable), instantiate if it's a `type`, store instance in state `LOADED`. Recursively loads `depends_on` with cycle detection. Idempotent. |
| `enable` | `async enable(plugin_id: str) -> PluginInstance` | Enable: auto-loads if needed, acquires VRAM when `vram_mb > 0`, transitions `LOADED→ENABLED` (or `→CRASHED` on VRAM failure). |
| `disable` | `async disable(plugin_id: str) -> None` | Disable: kills sandbox process if PROCESS, releases VRAM when `vram_mb > 0`, transitions to `DISABLED`. No-op if not loaded. |
| `unload` | `unload(plugin_id: str) -> None` | Drop the instance (releases VRAM if `vram_mb > 0`); registration retained. |
| `execute` | `async execute(plugin_id: str, params: dict, timeout_override: int \| None = None) -> Any` | Execute with timeout circuit-breaker. **Gate:** raises `RuntimeError` unless an instance exists **and** `state == ENABLED`. On timeout → `TIMEOUT` + auto-restart; on other exception → `CRASHED` + auto-restart. |
| `list_states` | `list_states() -> list[dict]` | Snapshot dicts for all loaded instances. |
| `get_state` | `get_state(plugin_id: str) -> dict \| None` | Snapshot for one plugin, or `None` if not loaded. |
| `list_by_state` | `list_by_state(state: PluginState) -> list[dict]` | Filtered list for instances matching `state`. |
| `start_watcher` / `stop_watcher` | `async start_watcher() -> None` / `async stop_watcher() -> None` | Start/stop the heartbeat-watcher background task. |

> **Note:** there is **no public `reload()`**. Crash recovery is the private `_maybe_restart` (unload → load → enable, gated by `max_restart ?? MAX_RESTART`). To force a fresh instance, `unload` then `enable`.

`PluginState` enum members: `REGISTERED`, `LOADED`, `ENABLED`, `DISABLED`, `CRASHED`, `TIMEOUT`.

## 9. Async, Timeouts, and Long Tasks

- **Async**: declare `async def my_plugin(desk, params)`; lifecycle detects it via `inspect.iscoroutinefunction` and awaits. See `examples/02_async/`.
- **Timeout**: `manifest.timeout_seconds` (or global `DEFAULT_TIMEOUT=600`, or `execute(..., timeout_override=)`). On timeout the plugin transitions to `TIMEOUT`, error count increments, and `_maybe_restart` may restart it (up to `max_restart`).
- **Long tasks**: declare `LONG_TASK` capability for the meltdown+restart path. See `examples/07_long_task/`.

## 10. Sandbox Modes: INLINE vs PROCESS

`manifest.sandbox_mode` (`SandboxMode` enum, `schema.py`):

| Mode | Behavior | Use when |
|---|---|---|
| `INLINE` (default) | Runs in-process via `_invoke_inline`. Supports async, closures, `VRAM_CONSUMER`. | Most plugins. |
| `PROCESS` | Runs in an isolated subprocess via `PluginSandbox` (IPC + heartbeat + `ResourceLimits`). Entry point **must be a module-level attribute** or `"module:attr"`. **Cannot acquire VRAM** (raises `NotImplementedError`). | Untrusted/heavy plugins needing crash isolation. |

Selection precedence in `_invoke` (`lifecycle.py:277`):
1. `manifest.sandbox_mode == PROCESS` → sandbox.
2. else if `config.sandbox_default_mode == "process"` → sandbox.
3. else → inline.

See `examples/08_process_sandbox/`.

## 11. Categories

`PluginCategory` (8 values):

| Category | Value | Examples |
|---|---|---|
| `CODING_PLAN` | `coding_plan` | coding-plan accelerators |
| `CONTEXT_COMPRESS` | `context_compress` | `caveman_compress` (built-in) |
| `MLX_INFERENCE` | `mlx_inference` | local MLX model inference |
| `TERMINAL_PROXY` | `terminal_proxy` | terminal command proxy |
| `FILE_INDEX` | `file_index` | local file search/index |
| `QUANTIZATION` | `quantization` | mixed-quantization tools |
| `VISUAL_BACKEND` | `visual_backend` | Claude image/visual generation backend |
| `CUSTOM` | `custom` | anything else |

`registry.list(category=...)` filters by it.

## 12. Registering & Running

```python
import fusion_plugins_ecosystem as fpe
from my_plugin_pkg import MY_ECHO_MANIFEST   # your manifest module

registry = fpe.PluginRegistry()
registry.register(MY_ECHO_MANIFEST)          # or registry.register_builtin()
lifecycle = fpe.PluginLifecycle(registry)

await lifecycle.enable("my_echo")            # load instance + state=ENABLED
result = await lifecycle.execute("my_echo", {"text": "hi"})   # run
await lifecycle.disable("my_echo")           # state=DISABLED
lifecycle.unload("my_echo")                  # drop instance (registration kept)
```

The execute gate: `lifecycle.execute` raises `RuntimeError("插件 ... 未启用")` unless `inst.state == ENABLED` (`lifecycle.py:248`). So **enable before execute**. (`default_mounted=True` + `config.default_mount_compressor` auto-enables at `MCPServer.start()`.)

### Standalone MCP server

```bash
fusion-plugin-server --transport stdio              # Claude Desktop subprocess
fusion-plugin-server --transport sse  --port 8765   # remote SSE
fusion-plugin-server --transport http --port 8765   # Streamable HTTP (MCP 2026-07-28)
```

`start()` registers built-ins, auto-mounts `default_mounted` plugins, blocks on a stop event (keep-alive), and serves `tools/list` + `tools/call`.

## 13. Reverse Path: Subagent Dispatch

fusion-cowork can pull a Claude Code subagent **to** run your plugin (reverse direction). Your plugin needs the `SUBAGENT` capability and (optionally) `agent_model`.

```python
import fusion_plugins_ecosystem as fpe

gw = fpe.ClaudeGateway(registry, lifecycle)
task = fpe.SubagentTask(
    name="batch-refactor",
    plugin_id="my_echo",
    arguments={"text": "refactor this"},
    timeout_seconds=120,
)
result = await gw.dispatch_subagent(task)
# → {"task": "batch-refactor", "plugin_id": "my_echo",
#    "state": "completed", "result": {...}}
```

`SubagentTask` fields: `name`, `plugin_id`, `arguments`, `timeout_seconds` (None→config), `metadata` (dict). On timeout/crash, `config.subagent_timeout_destroy` decides auto-destroy vs preserve. See `examples/04_subagent/`.

## 14. MCP Annotations & Output Schema

`MCPAnnotations` (`schema.py`, frozen dataclass) — MCP 2026-07-28 behavior hints:

```python
from fusion_plugins_ecosystem.schema import MCPAnnotations

manifest = PluginManifest(
    ...,
    mcp_annotations=MCPAnnotations(
        readOnlyHint=True,      # safe, no side effects
        destructiveHint=False,
        idempotentHint=True,    # repeat calls = same result
        openWorldHint=False,    # closed/known world
    ),
    output_schema={             # MCP outputSchema (JSON Schema)
        "type": "object",
        "properties": {
            "echo": {"type": "string"},
        },
        "required": ["echo"],
    },
)
```

Defaults: all hints `False` except `openWorldHint=True`.

## 15. Testing Checklist

- [ ] Manifest fields all set (`id`/`name`/`version`/`category`/`description`/`entry_point`).
- [ ] `params` types use `PluginParamType`; entry point reads via `params.get(name, default)`.
- [ ] Logs go through `desk.log(...)`, never `print()`.
- [ ] Async entry point declared `async def` (lifecycle auto-detects).
- [ ] `enable()` called before `execute()` (or `default_mounted=True`).
- [ ] PROCESS sandbox entry point is a module-level attribute or `"module:attr"` (no closures).
- [ ] `VRAM_CONSUMER` plugin uses `INLINE` mode (PROCESS can't acquire VRAM).
- [ ] Timeout set for long tasks; `LONG_TASK` capability declared.
- [ ] Return value is a plain `dict` (or dict with `content` list for custom MCP blocks).
- [ ] Test under `pytest` with `asyncio_mode=auto` (no `@pytest.mark.asyncio` needed). Clean temp data, keep only final outputs + logs.

## 16. Examples Index

Runnable scenarios in [`../examples/`](../examples/), each self-contained with manifest, test, and a README:

| # | Scenario | Teaches |
|---|---|---|
| 01 | `echo` | minimal sync plugin, `MCP_TOOL` + `CLAUDE_SKILL` |
| 02 | `async_fetch` | async entry point, auto-detection |
| 03 | `skill_export` | `CLAUDE_SKILL` → Skill bundle inspection |
| 04 | `subagent_reverse` | `SUBAGENT` capability + `dispatch_subagent` |
| 05 | `mlx_chat` | `MLX_INFERENCE`, `desk.mlx_chat()` |
| 06 | `vram_consumer` | `VRAM_CONSUMER` + `vram_mb`, INLINE only |
| 07 | `long_task` | `LONG_TASK` + timeout meltdown + restart |
| 08 | `process_sandbox` | `SandboxMode.PROCESS`, `module:attr` |
| 09 | `file_access` | `FILE_ACCESS` + `desk.check_file_permission` |
