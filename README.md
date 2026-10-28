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
  <img src="https://img.shields.io/badge/tests-689%20passed-success" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.4.7-blue" alt="Version">
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
# → 634 passed
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
│   ├── token_meter.py            ← unified token accounting + WAL append-write persistence
│   ├── metrics.py                ← observability: Prometheus counters/gauges + registry
│   ├── cluster_bridge.py         ← multi-node distributed state bridge (cowork cluster sync + failover)
│   ├── connector.py              ← external MCP connector framework (pack install, MCP client, HIGH_RISK approval gate)
│   ├── connector_packs.py        ← first-party connector packs (Feishu/DingTalk/WeCom/Mail/Calendar/Notion)
│   ├── marketplace.py            ← skill/connector pack marketplace (JSON index, install/uninstall)
│   ├── rest_connector.py         ← declarative REST/webhook connector docs (validation, read/write channels, execution contract)
│   ├── extensions_console.py     ← Extensions console backend (issue #30): Connectors + Plugins·MCP tabs, spine derivation, resync
│   ├── console_app.py            ← FastAPI console app + `fusion-plugin-console` CLI (port 8795)
│   ├── shell_registry.py         ← fusion-gateway shell module registration + fm- key persistence (#30)
│   ├── logging_setup.py          ← structured JSON logging + correlation_id (P3-6)
│   ├── config.py                 ← one-toggle config panel + observers
│   ├── hook_adapter.py           ← Claude Code Plugin hooks adapter
│   └── builtin/
│       ├── __init__.py
│       └── caveman_compress.py        ← built-in token compressor
├── web/
│   ├── extensions.html            ← console GUI (Connectors + Plugins·MCP tabs, verbatim layout from f-ecommerce)
│   ├── app.css                    ← fs-* design tokens (verbatim)
│   └── shell.js                   ← sidebar injector (brand "Fusion Plugins")
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
└── tests/                        ← 634 tests
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
    ├── test_product_audit.py        ← production audit P0-P3 fix regressions
    ├── test_metrics.py              ← observability metrics + /metrics endpoint
    ├── test_load.py                 ← load/concurrency safety (lifecycle semaphore, metrics thread-safety, sandbox spawn storm, token-meter pressure)
    ├── test_cluster_bridge.py       ← multi-node distributed state (cross-node plugin/vRAM visibility, failover evict, lifecycle sync, cluster-disabled no-op)
    ├── test_connector.py            ← connector framework: pack validation, registry bridge, HIGH_RISK approval gate, persistence, stdio/http MCP client round-trips
    ├── test_connector_packs.py      ← first-party packs structural validation + Feishu acceptance shape + lifecycle load
    ├── test_marketplace.py          ← marketplace: builtin catalog, JSON index load/override, install/uninstall, HTTPS-only constraint
    ├── test_rest_connector.py       ← declarative connector docs: validation, read/write channel split, persistence, execution contract vs local HTTP server
    ├── test_extensions_console.py   ← console backend: connector install/enable/harness, vault (Desk + local Fernet fallback), MCP intake probe/register/tick/call, plugin tick/remove/install, resync, spine derivation
    ├── test_console_app.py          ← FastAPI console: endpoint contract vs GUI, connector/MCP/plugin full flows, static serving
    ├── test_soak.py                 ← long-run stability (token-meter cap, log buffer cap, session LRU cap, counter monotonic no-drift, lifecycle sustained execute no-residual)
    └── test_integration.py
```

## 🔌 Connector framework (issue #27)

External MCP connector framework: fusion acts as an **MCP client** consuming arbitrary external MCP servers, with first-party domestic connector packs and a skill/connector marketplace.

```python
import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.connector_packs import get_builtin_pack

desk = fpe.DeskRuntime()
registry = fpe.PluginRegistry(desk)
creg = fpe.ConnectorRegistry(desk=desk, registry=registry,
                             persist_path="~/.fusion-ecosystem/connectors.json")
lifecycle = fpe.PluginLifecycle(registry)

# 1. Install a first-party pack (Feishu / DingTalk / WeCom / Mail / Calendar / Notion)
creg.install_pack(get_builtin_pack("feishu"))

# 2. Tools are bridged into the plugin registry → exposed as MCP tools / cowork nodes
#    e.g. plugin id "connector__feishu__im.message.send"

# 3. HIGH_RISK tools need explicit approval before execution
creg.approve_tool("feishu", "im.message.send")

# 4. Execute via lifecycle (routed through the external MCP server)
resp = await lifecycle.execute("connector__feishu__im.message.send",
                               {"chat_id": "oc_xxx", "content": "hello"})
```

Marketplace — distribute connector packs and cowork markdown skill packages:

```python
from fusion_plugins_ecosystem.marketplace import Marketplace

market = Marketplace(creg, marketplace_index="https://my-company/marketplace/index.json")  # private index
entries, warnings = market.load_index()   # builtin catalog + remote index (index overrides builtin on id collision)
market.install("feishu")                  # builtin pack: install directly
market.install("demo_skill")              # skill_package: git/local source → local install dir
market.list_installed()
```

Key properties:

- **Declarative packs** — a pack is pure data (transport/command/url + credential providers + tool specs with risk levels); the external MCP server command defaults are overridable
- **Credentials stay local** — providers resolved from `DeskRuntime.get_api_key` (Fernet-encrypted) at connect time; stdio → env vars, HTTP → `Authorization` header
- **Approval gating** — `HIGH` risk tools raise `PermissionError` until `approve_tool()`; all read-only tools are `LOW` risk by convention (enforced by tests)
- **INLINE-only** — connector tools run INLINE (the external MCP server is already an isolated process); `sandbox_default_mode=process` blocks installation
- **First-party packs**: Feishu (official `lark-mcp`), DingTalk, WeCom, Mail (IMAP/SMTP), Calendar (CalDAV), Notion (official `@notionhq/notion-mcp-server`)

## 🖥️ Extensions console (issue #30)

Standalone FastAPI console (`fusion-plugin-console`, port 8795) hosting the two tabs migrated
verbatim from f-ecommerce (issue #30, E3): **Connectors** and **Plugins·MCP**. Business logic
generalized — no e-commerce semantics remain.

```bash
pip install -e ".[console]"
fusion-plugin-console --port 8795     # → http://127.0.0.1:8795
```

- **Connectors tab** — declarative JSON connector packages (`rest_connector.py`): install =
  full static validation + disk persistence; enable = runtime bridging of READ channels (GET)
  as `connector__<id>__<op>` capability plugins (auto-listed, lifecycle ENABLED); WRITE
  channels (POST/PUT/PATCH/DELETE) enter a human review queue and are never auto-listed;
  encrypted credential vault (Desk Fernet store, falls back to console-local `vault.json`
  chmod 600 when Desk config_center is absent); harness = static validation + live check of
  the first read operation using vault credentials.
- **Plugins·MCP tab** — declarative plugin install (plugin.json manifest, no arbitrary code),
  tick/untick reconcile via lifecycle enable/disable, delete; remote MCP server probe & register
  (initialize + tools/list, candidate tools reported); per-tool tick bridges
  `mcp__<server>__<tool>` capability plugins dispatched over JSON-RPC `tools/call`.
- **Capability spine** — derived from disk facts (connector docs + MCP ticks), rebuilt at boot
  via `resync()`; in-process registry holds no second source of truth.
- **Endpoints** — `/api/whoami`, `/api/capabilities`, `/api/extensions/connectors[/{id}/enable|disable|harness]`,
  `/api/adapters/{ref}` (vault), `/api/extensions/plugins[/{name}/tick]`, `/api/extensions/mcp[/{sid}/tick]`, `/health`.
- **Env vars** — `FUSION_PLUGIN_CONNECTOR_DIR` (default `~/.fusion-plugins-ecosystem/connectors`),
  `FUSION_PLUGIN_EXTENSIONS_DIR` (default `~/.fusion-plugins-ecosystem/extensions`),
  `FUSION_PLUGIN_KEY_SALT` (key-derivation salt; unset = empty in dev with a warning,
  fail-fast under `FUSION_PROFILE=prod` — no default literal, see
  `architecture/config-conventions.md` §3),
  `FUSION_PLUGINS_ECOSYSTEM_DATA_DIR` (issue #38 single-point data dir via
  `fusion_core.config.service_data_dir`; default `~/.fusion-plugins-ecosystem`),
  `FUSION_PLUGINS_ECOSYSTEM_PORT` / `FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT`
  (issue #38 port resolution via `fusion_core.config.service_port`).

### Process titles (issue #35)

Long-running entry points set their process title via `setproctitle` (base dependency,
silent no-op fallback if unavailable): `fusion-plugin-server stdio`,
`fusion-plugin-server sse :8765`, `fusion-plugin-console :8795` — `ps`/Activity Monitor
show the product name and role instead of a bare `Python`.

### Shell module (gateway registration, issue #30 follow-up)

The console registers itself as a fusion-gateway shell module — the shell sidebar shows an
**Extensions Console** entry at `/shell/fusion-plugins-ecosystem/`; stopping the console greys
the entry (heartbeat expiry), restarting re-registers it. Same pattern as fusion-cowork #97/#168.

```bash
FUSION_ADMIN_KEY=<web_shell.admin_key> \
fusion-plugin-console --port 8795
```

- **Registration** — `shell_registry.py` POSTs `web/manifest.json` (id `fusion-plugins-ecosystem`,
  upstream `http://127.0.0.1:8795`) then heartbeats every 15s; gateway restart (404) or key
  rotation (401) triggers transparent re-registration. Registration is OFF unless `FUSION_ADMIN_KEY`
  resolves (dev: missing key = empty + warning; prod: missing key = `RuntimeError` fail-fast).
  The gateway address resolves via `fusion_core.config.gateway_base_url()`
  (`FUSION_GATEWAY_BASE_URL`, default `http://127.0.0.1:11432`) — issue #38 single-point resolution.
- **fm- module key (K1/K4)** — with no static api_key in the registration body the gateway issues
  an `fm-` service key (returned exactly once), persisted 0600 at
  `~/.fusion-plugins-ecosystem/web_module_key`; heartbeats authenticate with it.
- **API auth** — when a key exists (static `FUSION_PLUGINS_CONSOLE_API_KEY` and/or issued `fm-` key,
  dual-active), `/api/*` requires `Authorization: Bearer` / `x-api-key` (the gateway proxy injects
  the module key); with no keys configured the API stays open for loopback-only use. Static pages
  are session-gated at the gateway, not upstream.
- **Path-prefix aware GUI** — `extensions.html`/`shell.js` resolve the API base from
- **Theme follow (#33)** — embedded in the shell the iframe follows the host theme push (`postMessage` `{type:"event", event:"theme"}`, same-origin only); standalone reads the initial mode from `localStorage["fusion.shell.theme"]` (legacy `fs_theme` cookie + `prefers-color-scheme` fallbacks) and the sidebar toggle persists to both.
  `location.pathname`, so the same build works standalone (`/`) and behind the gateway
  (`/shell/fusion-plugins-ecosystem/`).

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
| `enable_connector_framework` | External MCP connector install/bridge | install raises `RuntimeError` |
| `connector_persist_path` | Connector registry persistence file | `None` = in-memory |
| `connector_marketplace_index` | Marketplace index (local JSON path or HTTPS URL) | `None` = builtin catalog only |
| `vram_limit_mb` | Total vRAM budget (synced to `DeskRuntime.vram_total_mb`) | `0` = unlimited |
| `max_concurrent_plugins` | Inline execution concurrency gate (`asyncio.Semaphore`) | unbounded |
| `log_level` | Root logger level (applied on `MCPServer` init) | `INFO` |
| `subagent_timeout_seconds` | Subagent execution timeout | `600` |
| `max_auto_restart` | Crash auto-restart cap | `3` |
| `heartbeat_stale_seconds` | Heartbeat staleness threshold | `120` |

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

## 🛡️ Hardening (audit P0–P3)

A full architecture audit (`audit/fusion-plugins-ecosytem--audit-result-0830.md`) surfaced
20 P0–P3 findings; all fixable in-repo are addressed:

| ID | Finding | Fix |
|----|---------|-----|
| A3 | Studio config keys semantically reversed | `log_level`/`vram_limit_mb`/`max_concurrent_plugins` map to real backend fields (no longer to `mcp_transport`/`mcp_port`/`max_auto_restart`) |
| R6 | SSE/HTTP zero-auth + plaintext API keys | Bearer-token auth (`FUSION_PLUGIN_AUTH_TOKEN` env, constant-time compare); API keys at-rest Fernet-encrypted (`enc:` prefix, plaintext passthrough for back-compat) |
| A4 | config/install zero-persistence | `plugins/config.set`, `plugins/install`, `plugins/uninstall` persist to Desk `config_center`; `MCPServer.start()` restores installed plugin set |
| R1 | PROCESS sandbox `_DeskProxy` no-op | Worker → host resource-proxy RPC (`acquire_vram`/`release_vram`/`mlx_chat`/`get_api_key`/`check_file_permission`) over stdin/stdout, thread-safe future resolution |
| A2 | config → lifecycle break, dead fields | `MCPServer`/`MCPHandler` inject `config` into `PluginLifecycle`; timeout/restart/heartbeat thresholds config-driven with class-constant fallback |
| A5 | token_meter ↔ config break | `MCPHandler`/`MCPServer` construct `TokenMeter` with `max_token_records`/`token_persist_path` from config |
| R2 | inline unbounded thread pool | `asyncio.Semaphore` (`max_concurrent_plugins`) gates `to_thread` inline execution |
| R3 | `_maybe_restart` no backoff | exponential backoff `min(2**(n-1), 60)` (skipped when no config, preserving test immediacy); double-check plugin existence post-backoff; enable failure → CRASHED |
| R4 | token_meter full-rewrite per record | throttled persistence (batch/interval) + explicit `flush()` |
| R5 | per-handler rate limit | session + rate-limit state shared on `DeskRuntime` (cross-handler single source, lock-guarded) |
| R7 | log lock contention | `get_logs` snapshots under lock, filters outside |
| E1 | spawn passes empty config | `_invoke_sandbox` forwards `config.to_dict()` to worker |
| E2 | `cpu_limit` decorative | `RLIMIT_CPU` enforced in worker (SIGXCPU on exceed) |
| E3 | unload fire-and-forget | kill task tracked in `_pending_kill_tasks` to prevent GC |
| E4 | `dispatch_subagent` uncaught enable exception | enable errors caught, logged, returned as `failed` state |
| E5 | version string compare not semver | `_version_key` tuple parse (`"1.0"` == `"1.0.0"`) |
| E6 | in-function repeated `import json` | hoisted to module top (`jsonrpc.py`, `claude_gateway.py`) |
| E7 | `claude_adapter` legacy double logic | SSOT `build_skill_dict` in `skill_adapter.py`, legacy adapter delegates |
| E8 | tests mask config break | `_make_handler` accepts `config`; wiring test asserts config drives lifecycle/token_meter |
| A1 | zero cluster/multi-node awareness | **out of scope** — requires cross-project distributed state layer; upstream issue [fusion-cowork#79](https://github.com/dahai80/fusion-cowork/issues/79) |

### Remote transport auth

SSE/HTTP transports are remote-capable and therefore require a Bearer token when exposed
beyond localhost. Set it via environment variable (never persisted to config):

```bash
FUSION_PLUGIN_AUTH_TOKEN="$(openssl rand -hex 32)" fusion-plugin-server --transport http --port 8765
```

Requests without `Authorization: Bearer <token>` get `401 Unauthorized`. stdio transport
(local subprocess) is exempt.

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

Latest run: **634 passed**.

| Test file | Tests | Covers |
|-----------|-------|--------|
| `test_desk_runtime.py` | 30 | vRAM / logging / permissions / API keys / MLX / node bridge / scheduler bridge |
| `test_lifecycle.py` | 40 | load/enable/disable/unload/execute/timeout/crash/restart/watcher/idempotency + _enable_with_retry (vRAM transient retry, permanent-error no-retry) |
| `test_mcp_exporter.py` | 13 | list_tools / call_tool / gateway_info / manifest_to_mcp_tool |
| `test_token_meter.py` | 22 | TokenRecord / record / measure / summary / stuck-subagent warning / WAL append+compaction (round-trip, crash-recovery dedup, threshold) |
| `test_claude_adapter.py` | 13 | export_one / export_all / export_default_mounted / param types / enum / required |
| `test_claude_gateway.py` | 45 | three-layer compat / forward-reverse / auth / config toggles / token integration |
| `test_config.py` | 9 | defaults / to_dict / from_dict / roundtrip / partial / unknown key |
| `test_registry_full.py` | 15 | register / unregister / list / category / default_mounted / enum values |
| `test_caveman.py` | 22 | _compress_text / caveman_compress / CAVEMAN_MANIFEST fields |
| `test_registry.py` | 13 | (legacy) registry + adapter + exporter + caveman integration |
| `test_hook_adapter.py` | 8 | HookAdapter event mapping / capability filtering |
| `test_transport_server.py` | 27 | SSE/HTTP/stdio transport + MCPServer start/stop lifecycle + CLI main() + SSE queue-bound drop-on-full |
| `test_jsonrpc_plugins.py` | 25 | Studio `plugins/*` 15-method dict envelopes + exact-key matching + config-driven wiring (E8) |
| `test_metrics.py` | 20 | observability counters/gauges / Prometheus render / label escaping / `/metrics` endpoint / path counters (lifecycle/MCP/sandbox/vram/session) |
| `test_load.py` | 8 | load/concurrency safety: lifecycle concurrent execute + max_concurrent semaphore, metrics thread-safety (counter/gauge/render), token-meter high-freq + concurrent record, PROCESS sandbox spawn storm no-leak |
| `test_cluster_bridge.py` | 9 | multi-node distributed state: cross-node plugin enable/disable + vRAM visibility, stale-node failover evict (clears state/vRAM), heartbeat keeps-alive, cluster_bridge singleton sync, lifecycle enable/disable cluster sync, DeskRuntime cluster query methods, cluster-disabled no-op degradation |
| `test_soak.py` | 8 | long-run stability: token-meter cap holds under 50k records, log buffer deque cap, session _MAX_SESSIONS LRU cap + calls-list bound, counter 100k monotonic inc no-drift (labeled buckets), lifecycle 500 sustained execute no-residual, token-meter 8k record + query aggregate consistent |
| `test_logging_setup.py` | 10 | structured JSON logging: JsonFormatter output + correlation_id propagation (set/reset/contextvar), configure idempotent + extras + exc_info + non-serializable fallback (P3-6) |
| `test_connector.py` | 25 | connector framework: pack validation/roundtrip, registry bridge + MCP export, HIGH_RISK approval gate, persistence roundtrip, credential resolution, real stdio MCP client subprocess round-trip, local HTTP JSON-RPC round-trip |
| `test_connector_packs.py` | 10 | first-party packs structural validation, unique ids/tool names, schema shape, risk-level conventions (read-only=LOW, write=MEDIUM/HIGH), Feishu acceptance shape, lifecycle load of bridged tools |
| `test_marketplace.py` | 13 | builtin catalog, JSON index load + same-id override, HTTPS-only constraint, install builtin/local-source/skill-package, uninstall, list_installed, config wiring |
| `test_rest_connector.py` | 15 | declarative connector docs: validation, read/write channel split, persistence roundtrip, execution contract vs local HTTP server, JSONPath-lite extraction, `{var}` rendering |
| `test_extensions_console.py` | 26 | console backend: connector install/enable/disable/harness, credential vault (Desk round-trip + local Fernet fallback), bridged op execution, MCP intake probe/register/tick/call, plugin tick/remove/install, resync rebuild, spine derivation, standalone lifecycle bootstrap |
| `test_console_app.py` | 6 | FastAPI console: whoami/capabilities, connector full flow (install→vault→enable→disable→harness), MCP flow, plugins flow, static serving |

## ⚠️ Technical constraints

- **Cannot run standalone** — process hosting, permissions, logs, resources all depend on `fusion-cowork`
- **Python ≥ 3.11** — aligned with fusion-cowork
- **Mac M-series native** — depends on fusion-mlx Metal/MLX acceleration
- **No direct `fusion_cowork` internal imports** — go through `DeskRuntime`

## 📄 License

Apache License 2.0 — part of the [Fusion-MLX](https://github.com/fusion-mlx) Apple Silicon local AI ecosystem.

