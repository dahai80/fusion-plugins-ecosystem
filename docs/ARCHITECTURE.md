# Architecture

> How `fusion-plugins-ecosystem` sits on top of `fusion-desk` and wires every plugin capability back to Claude.

## 1. Layering

```
fusion-plugin-ecosystem        ← this package: registry, lifecycle, Claude adaptation
        ↓ depends on API
fusion-desk runtime            ← base runtime: MCP gateway, hardware, sessions, logging
        ↓
fusion-mlx core                ← Mac local inference kernel (Metal/MLX)
```

`fusion-plugin-ecosystem` is **not** a standalone project. Process hosting, permission control, log collection, and resource throttling are all provided by `fusion-desk`. This package only adds:

1. **Plugin registry** — declarative manifests, param schemas, capability declarations
2. **Lifecycle manager** — load/unload/enable/disable/hot-reload, timeout meltdown, auto-restart
3. **Claude full-chain adaptation** — plugins auto-convert to Claude Skills, expose as MCP Tools
4. **Token metering** — split Claude model consumption vs plugin local compute
5. **Desk context bridge** — reuse Desk's MCP gateway, hardware scheduler, session pool

## 2. Module map

| Module | Role | Key symbols |
|--------|------|-------------|
| `desk_runtime.py` | fusion-desk runtime handle wrapper | `DeskRuntime` |
| `desk_context.py` | thin bridge delegating to `DeskRuntime` | `DeskContext` |
| `registry.py` | declarative plugin registry | `PluginRegistry`, `PluginManifest`, `PluginCapability`, `PluginCategory`, `PluginParam` |
| `lifecycle.py` | load/enable/execute + meltdown + restart | `PluginLifecycle`, `PluginState`, `PluginInstance` |
| `token_meter.py` | unified token accounting by kind | `TokenMeter`, `TokenRecord`, `TokenKind` |
| `claude_adapter.py` | plugin → Claude Skill | `ClaudeSkillAdapter` |
| `mcp_exporter.py` | plugin → MCP Tools | `MCPExporter` |
| `claude_gateway.py` | unified Claude full-chain gateway | `ClaudeGateway`, `SubagentTask`, `CLAUDE_DESKTOP/CODE/WEB/VOLCENGINE` |
| `config.py` | one-toggle config panel | `EcosystemConfig` |
| `builtin/caveman_compress.py` | built-in token compressor | `CAVEMAN_MANIFEST`, `caveman_compress` |

## 3. Data flow: forward (Claude calls fusion)

```
Claude Desktop / Claude Code / Web Claude
        │  MCP tools/call
        ▼
ClaudeGateway.invoke_mcp_tool(plugin_id, args)
        │  config.enable_claude_mcp gate
        ▼
MCPExporter → PluginLifecycle.execute(plugin_id, args)
        │                         │
        ▼                         ▼
DeskContext.acquire_vram    TokenMeter.measure(MCP_RELAY)
        │
        ▼
plugin entry point(desk, params) → result
        │
        ▼
MCP tools/call response (isError=False, content=[{type:text, text:json}]）
```

## 4. Data flow: reverse (fusion pulls Claude Code subagent)

```
fusion-desk runtime
        │  SubagentTask(name, plugin_id, arguments, timeout_seconds)
        ▼
ClaudeGateway.dispatch_subagent(task)
        │  config.subagent_timeout_seconds + subagent_timeout_destroy gate
        ▼
PluginLifecycle.enable → execute(plugin_id, args)
        │                  │
        ▼                  ▼
DeskContext.log          TokenMeter.measure(PLUGIN_LOCAL)
        │
        ▼
result OR on timeout/crash → unload (auto-destroy) → failed state
```

## 5. Capability → exposure mapping

| `PluginCapability` | Auto exposure | Adapter |
|--------------------|---------------|---------|
| `MCP_TOOL` | MCP tools/list item | `MCPExporter` |
| `CLAUDE_SKILL` | Claude Skill dict | `ClaudeSkillAdapter` |
| `SUBAGENT` | Listed in `list_subagent_capable_plugins` | `ClaudeGateway` |
| `FILE_ACCESS` | Desk permission check | `DeskContext.check_file_permission` |
| `VRAM_CONSUMER` | Desk vRAM budget ledger | `DeskRuntime.acquire_vram` |
| `LONG_TASK` | Timeout meltdown + auto-restart | `PluginLifecycle.execute` |

## 6. Pain points solved

| Pain point (from PRD) | Solution |
|-----------------------|----------|
| Subagent runs 40 min with zero token consumption, no logs | `TokenMeter` warns on `wall_seconds>60 ∧ total_tokens==0`; all logs route via `DeskContext.log` |
| vRAM contention across plugins | `DeskRuntime.acquire_vram` enforces `vram_total_mb` budget ledger |
| MCP port conflicts | fusion-desk single MCP gateway; `MCPExporter` multiplexes |
| Subagent hangs with no unified restart | `PluginLifecycle` timeout meltdown + `_maybe_restart` (≤ `MAX_RESTART`) |
| Heartbeat stall detection | `PluginLifecycle._watch_loop` flags `HEARTBEAT_STALE` → `TIMEOUT` |

## 7. Non-goals

- This package does **not** spawn processes directly — `fusion-desk`'s sandbox does.
- This package does **not** own the MCP wire protocol — `fusion-desk`'s MCP gateway does.
- This package does **not** load MLX models — `fusion-mlx` does; we only call `DeskContext.mlx_chat`.
