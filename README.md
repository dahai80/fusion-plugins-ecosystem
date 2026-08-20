<div align="center">
  <h1>🔌 Fusion-Plugins-Ecosystem</h1>
  <p><strong>Plugin registry, lifecycle manager, and native Claude full-chain adaptation layer for fusion-cowork.</strong></p>
  <p><em>Build on fusion-cowork. Expose every plugin to Claude. Zero adapter code.</em></p>
  <p>English | <a href="README_CN.md">简体中文</a></p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/base-fusion--cowork-orange" alt="fusion-cowork">
  <img src="https://img.shields.io/badge/Claude-native-blueviolet" alt="Claude">
  <img src="https://img.shields.io/badge/MCP-2026--07--28-yellow" alt="MCP">
  <img src="https://img.shields.io/badge/tests-449%20passed-success" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.3.4-blue" alt="Version">
  <img src="https://img.shields.io/badge/coverage-89%25-success" alt="Coverage">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
</p>

---

# Fusion-Plugins-Ecosystem

> Plugin registry, lifecycle manager, and native Claude full-chain adaptation layer built on `fusion-cowork`. Every plugin auto-exposes as a Claude Skill and an MCP Tool. Zero per-plugin adapter code.

**简体中文版见 [README_CN.md](README_CN.md)。**

## 📋 Overview

`fusion-plugins-ecosystem` is the **upper-layer submodule** of `fusion-cowork`. It is **not** a standalone project — process hosting, permission control, log collection, and resource throttling are all provided by `fusion-cowork`. This package only adds:

1. **Plugin registry** — declarative manifests, param schemas, capability declarations
2. **Lifecycle manager** — load/unload/enable/disable/hot-reload, timeout meltdown, auto-restart, INLINE/PROCESS sandbox modes
3. **Claude full-chain adaptation** — plugins auto-convert to Claude Code Skills, Agents, Plugin Bundles; expose as MCP Tools (2026-07-28)
4. **Token metering** — split Claude model consumption vs plugin local compute, JSON persistence
5. **MCP protocol stack** — stdio/SSE/HTTP transports, JSON-RPC 2.0 handler, MCP Server CLI
6. **Plugin sandbox** — process isolation via subprocess IPC, resource limits, heartbeat monitoring
7. **Cowork context bridge** — reuse fusion-cowork's MCP gateway, hardware scheduler, session pool

### Architecture

```
fusion-plugin-ecosystem        ← this package: registry, lifecycle, Claude adaptation, sandbox, MCP server
        ↓ depends on API
fusion-cowork runtime          ← base runtime: MCP gateway, hardware, sessions, logging
        ↓
fusion-mlx core                ← Mac local inference kernel (Metal/MLX)
```

### Claude three-layer compatibility

| Access | Constant | Gateway entry |
|--------|----------|---------------|
| Claude Desktop client | `CLAUDE_DESKTOP` | `list_mcp_tools` + `gateway_info` (stdio transport) |
| VS Code Claude Code plugin | `CLAUDE_CODE` | `dispatch_subagent` + `list_subagent_capable_plugins` |
| Web Claude | `CLAUDE_WEB` | `invoke_mcp_tool` (HTTP/SSE relay) |
| Volcengine Claude Coding Plan | `CLAUDE_VOLCENGINE` | `store_credentials("volcengine_claude", ...)` |

**Bidirectional**:
- **Forward**: Claude calls all fusion local capabilities (image/video generation, MLX local inference, file ops, quantization tools)
- **Reverse**: fusion-cowork proactively spins up Claude Code subagents for batch refactors, PR generation, code optimization

See [docs/CLAUDE_COMPATIBILITY.md](docs/CLAUDE_COMPATIBILITY.md) for the full spec.

## 🚀 Quick Start

### Installation

```bash
# Clone
git clone https://github.com/dahai80/fusion-plugins-ecosystem.git
cd fusion-plugins-ecosystem

# Create venv (avoid PEP 668)
python3 -m venv .venv
.venv/bin/pip install -e ../fusion-cowork    # base runtime (not on PyPI)
.venv/bin/pip install -e ".[test]"

# Run tests
.venv/bin/python -m pytest --cov=fusion_plugins_ecosystem --cov-report=term-missing -q
# → 449 passed
```

```python
import fusion_plugins_ecosystem as fpe

# Registry + built-in caveman compressor
registry = fpe.PluginRegistry()
registry.register_builtin()

# Claude full-chain gateway
lifecycle = fpe.PluginLifecycle(registry)
gw = fpe.ClaudeGateway(registry, lifecycle)

# Forward: expose to Claude
skills = gw.export_skills()           # → Claude Skill dicts
tools = gw.list_mcp_tools()           # → MCP tool descriptors

# Forward: Claude calls via MCP
resp = await gw.invoke_mcp_tool(
    "caveman_compress", {"text": "# comment\ncode"}
)
# → {"content": [{"type":"text","text":"{\"compressed\":\"code\",...}"}], "isError": false}

# Reverse: pull Claude Code subagent
task = fpe.SubagentTask(
    name="compress-session",
    plugin_id="caveman_compress",
    arguments={"text": "a\n\n\n\nb"},
)
result = await gw.dispatch_subagent(task)
# → {"state": "completed", "result": {"compressed": "a\n\nb", ...}}
```

## 🧩 Writing a plugin

```python
from fusion_plugins_ecosystem.registry import (
    PluginCapability, PluginCategory, PluginManifest, PluginParam,
)

def my_plugin(desk, params):
    desk.log("my_plugin", "INFO", "called", chars=len(params["text"]))
    return {"echo": params["text"].upper()}

MY_MANIFEST = PluginManifest(
    id="my_echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Echoes input text uppercased.",
    capabilities=[
        PluginCapability.MCP_TOOL,        # auto-expose as MCP Tool
        PluginCapability.CLAUDE_SKILL,    # auto-convert to Claude Skill
    ],
    params=[
        PluginParam(name="text", type="string", description="Input", required=True),
    ],
    entry_point=my_plugin,
    timeout_seconds=60,
)
```

Register once → it auto-appears in Claude's tool catalog and MCP `tools/list`. No adapter code. See [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md).

## 📚 Documentation

| Doc | Content |
|-----|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, module map, data flow (forward/reverse), capability mapping, pain points solved |
| [docs/API.md](docs/API.md) | Full public API reference: `PluginRegistry`, `PluginLifecycle`, `ClaudeGateway`, `TokenMeter`, `DeskRuntime`, `EcosystemConfig` |
| [docs/CLAUDE_COMPATIBILITY.md](docs/CLAUDE_COMPATIBILITY.md) | Three-layer compat, forward/reverse flows, volcengine auth, MLX visual backend, token metering, config toggles |
| [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md) | Plugin authoring guide: manifest, capabilities, params schema, async, lazy entry, testing checklist |
| [examples/](examples/) | 9 runnable plugin samples (echo → file_access): each a manifest + test + README. `pytest examples/` |

## 🗂️ Project structure

```
fusion-plugins-ecosystem/
├── pyproject.toml
├── LICENSE                       ← Apache 2.0
├── README.md                     ← this file (English)
├── README_CN.md                  ← 中文版
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API.md
│   ├── CLAUDE_COMPATIBILITY.md
│   └
    PLUGIN_DEVELOPMENT.md
├── fusion_plugins_ecosystem/
│   ├── __init__.py               ← top-level exports + Lazy Import
│   ├── desk_runtime.py           ← fusion-cowork runtime handle wrapper
│   ├── registry.py               ← plugin registry + frozen manifest
│   ├── lifecycle.py              ← load/enable/execute + meltdown + restart + INLINE/PROCESS
│   ├── sandbox.py                ← plugin sandbox (process isolation, IPC)
│   ├── transport.py              ← MCP transport layer (stdio/SSE/HTTP)
│   ├── jsonrpc.py                ← MCP JSON-RPC 2.0 handler
│   ├── server.py                 ← MCP Server entry + CLI
│   ├── schema.py                 ← shared schema (SandboxMode, MCPAnnotations, types)
│   ├── skill_adapter.py          ← plugin → Claude Code Skill bundle
│   ├── agent_adapter.py          ← plugin → Claude Code Agent .md
│   ├── plugin_bundle.py          ← Claude Code Plugin bundle generator
│   ├── claude_adapter.py         ← legacy adapter (backward compat)
│   ├── mcp_exporter.py           ← plugin → MCP Tools
│   ├── claude_gateway.py         ← unified Claude full-chain gateway
│   ├── token_meter.py            ← unified token accounting + persistence
│   ├── config.py                 ← one-toggle config panel + observers
│   ├── hook_adapter.py           ← Claude Code Plugin hooks adapter
│   └
    builtin/
│       ├── __init__.py
│       └
        caveman_compress.py        ← built-in token compressor
├── examples/                      ← 9 runnable plugin samples (ex01_echo … ex09_file_access)
│   ├── README.md                  ← scenario index + run instructions
│   ├── ex01_echo/                 ← minimal sync plugin
│   ├── ex02_async/                ← async entry point
│   ├── ex03_skill_export/         ← Skill bundle export
│   ├── ex04_subagent/             ← reverse subagent dispatch
│   ├── ex05_mlx_chat/             ← local MLX inference
│   ├── ex06_vram/                 ← VRAM consumer (INLINE)
│   ├── ex07_long_task/            ← timeout meltdown + restart
│   ├── ex08_process_sandbox/      ← PROCESS sandbox isolation
│   └── ex09_file_access/          ← file permission gating
└── tests/                        ← 449 tests
    ├── test_caveman.py
    ├── test_claude_adapter.py
    ├── test_claude_gateway.py
    ├── test_config.py
    ├── test_desk_runtime.py
    ├── test_lifecycle.py
    ├── test_mcp_exporter.py
    ├── test_registry.py
    ├── test_registry_full.py
    ├── test_token_meter.py
    ├── test_jsonrpc.py
    ├── test_jsonrpc_plugins.py
    ├── test_transport_server.py
    ├── test_sandbox.py
    ├── test_hook_adapter.py
    ├── test_phase3_adapters.py
    ├── test_phase4_meter_config.py
    ├── test_schema.py
    └── test_integration.py
```

## 🔧 Configuration (one-click panel)

`EcosystemConfig` — all toggles default to `True` for native Claude compatibility.

| Toggle | Controls | Off behavior |
|--------|----------|--------------|
| `enable_claude_mcp` | `list_mcp_tools`, `invoke_mcp_tool` | returns `[]` / `isError=True` |
| `auto_export_claude_skill` | `export_skills` | returns `[]` |
| `default_mount_compressor` | `export_default_mounted_skills` | returns `[]` |
| `subagent_timeout_destroy` | `dispatch_subagent` | no unload on timeout |
| `enable_volcengine_claude_plan` | volcengine credential store/get | refused / `None` |
| `enable_mixed_quantization` | `mlx_visual_backend` | raises `RuntimeError` |
| `mcp_transport` | MCP transport type | `stdio` / `sse` / `http` |
| `sandbox_default_mode` | Default sandbox mode | `inline` / `process` |
| `max_token_records` | Max token meter records | pruning threshold |
| `token_persist_path` | Token persistence file | `None` = in-memory |

```python
from fusion_plugins_ecosystem import EcosystemConfig

# All-on by default
config = EcosystemConfig()
# Disable MCP exposure
config.enable_claude_mcp = False
# Persist via Desk config center
d = config.to_dict()
restored = EcosystemConfig.from_dict(d)
```

## 🛠️ Pain points solved

| Pain point (from PRD) | Solution |
|-----------------------|----------|
| Subagent runs 40 min, zero token consumption, no logs | `TokenMeter` warns on `wall>60 ∧ total=0`; all logs via `DeskRuntime.log` |
| vRAM contention across plugins | `DeskRuntime.acquire_vram` enforces `vram_total_mb` budget ledger |
| MCP port conflicts | fusion-cowork single MCP gateway; `MCPExporter` multiplexes |
| Subagent hangs, no unified restart | `PluginLifecycle` timeout meltdown + `_maybe_restart` (≤ `MAX_RESTART`) |
| Heartbeat stall | `PluginLifecycle._watch_loop` flags `HEARTBEAT_STALE` → `TIMEOUT` |

## 🖥️ fusion-studio integration

`fusion-studio` consumes this package over HTTP JSON-RPC. The SwiftUI client
(`PluginBridge.swift`) POSTs `{"jsonrpc":"2.0",...}` to `http://<host>:<port>/rpc`
and reads `json["result"]` as a **dict with named keys** (not an array). The
`MCPHandler.handle` dispatcher exposes 15 `plugins/*` methods whose result
envelopes are shaped to match each `PluginEcosystemModels.swift` `fromDict`
contract exactly:

| Method | Result envelope | Studio model |
|--------|-----------------|--------------|
| `plugins.ping` | `{pong}` | health check |
| `plugins/list` | `{plugins[]}` | `PluginListItem` (id/name/category/version/description/author/enabled/installed) |
| `plugins/install` | `{ok}` | enable plugin |
| `plugins/uninstall` | `{ok}` | disable + unload |
| `plugins/config.get` | 7 Studio-named keys | `EcosystemConfig` (sandbox_mode/auto_update/max_concurrent_plugins/log_level/token_budget/vram_limit_mb/mcp_enabled) |
| `plugins/config.set` | `{ok}` | single key-value pair (params IS the pair) |
| `plugins/states` | `{states[]}` | `PluginStateInfo` (id/plugin_id/state/pid/start_time:str/uptime:int/error_count/last_error) |
| `plugins/state.get` | state dict | `PluginStateInfo` |
| `plugins/state.list` | `{plugins[]}` | filtered by state |
| `plugins/token.records` | `{records[]}` | `TokenRecord` (id/plugin_id/prompt_tokens/completion_tokens/total_tokens/timestamp:str/model) |
| `plugins/token.prune` | `{ok}` | prune by `max_age_seconds` |
| `plugins/vram.usage` | `{total_mb,used_mb,free_mb,by_plugin[]}` | `VRAMUsage` + `VRAMPluginEntry` (allocated_mb/peak_mb) |
| `plugins/logs.stream` | `{entries[]}` | `PluginLogEntry` (id:str/plugin_id/level/message/timestamp:str) |
| `plugins/mcp.sessions` | `{sessions[]}` | `MCPSession` (id/session_id/plugin_id/server/status/tool_count/connected_at:str) |
| `plugins/mcp.sessions.prune` | `{ok}` | prune by `max_age_seconds` |

The backend `EcosystemConfig` uses different field names than Studio; the
`plugins/config.*` handlers project between the two namespaces. `MCPServer`
auto-registers built-ins (`caveman_compress`) on `start()`, so
`plugins/list` is discoverable without manual registration. Node discovery
(`desk.list_nodes()`) returns `[]` when `fusion-cowork` is not installed —
the integration path requires `fusion-cowork` to host the `/rpc` endpoint
(upstream gap, tracked separately).

`MCPServer.start()` blocks on an instance-level `stop_event` for **all**
transports (stdio/sse/http), so the standalone CLI (`fusion-plugin-server`)
stays alive until `stop()` or SIGTERM — previously sse/http returned
immediately and the process exited. Verified: `fusion-plugin-server
--transport http` stays alive and `/rpc` returns `caveman_compress` v0.3.4
and `{pong: true}`.

## 🧪 Testing

```bash
.venv/bin/python -m pytest --cov=fusion_plugins_ecosystem --cov-report=term-missing -q
```

Latest run: **449 passed**.

| Test file | Tests | Covers |
|-----------|-------|--------|
| `test_desk_runtime.py` | 30 | vRAM / logging / permissions / API keys / MLX / node bridge / scheduler bridge |
| `test_lifecycle.py` | 24 | load/enable/disable/unload/execute/timeout/crash/restart/watcher |
| `test_mcp_exporter.py` | 13 | list_tools / call_tool / gateway_info / manifest_to_mcp_tool |
| `test_token_meter.py` | 16 | TokenRecord / record / measure / summary / stuck-subagent warning |
| `test_claude_adapter.py` | 13 | export_one / export_all / export_default_mounted / param types / enum / required |
| `test_claude_gateway.py` | 45 | three-layer compat / forward-reverse / auth / config toggles / token integration |
| `test_config.py` | 9 | defaults / to_dict / from_dict / roundtrip / partial / unknown key |
| `test_registry_full.py` | 15 | register / unregister / list / category / default_mounted / enum values |
| `test_caveman.py` | 22 | _compress_text / caveman_compress / CAVEMAN_MANIFEST fields |
| `test_registry.py` | 13 | (legacy) registry + adapter + exporter + caveman integration |
| `test_hook_adapter.py` | 8 | HookAdapter event mapping / capability filtering |
| `test_transport_server.py` | 25 | SSE/HTTP/stdio transport + MCPServer start/stop lifecycle + CLI main() |
| `test_jsonrpc_plugins.py` | 23 | Studio `plugins/*` 15-method dict envelopes + exact-key matching |

## ⚠️ Technical constraints

- **Cannot run standalone** — process hosting, permissions, logs, resources all depend on `fusion-cowork`
- **Python ≥ 3.11** — aligned with fusion-cowork
- **Mac M-series native** — depends on fusion-mlx Metal/MLX acceleration
- **No direct `fusion_cowork` internal imports** — go through `DeskRuntime`

## 📄 License

Apache License 2.0 — part of the [Fusion-MLX](https://github.com/fusion-mlx) Apple Silicon local AI ecosystem.

