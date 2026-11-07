---
doc_id: fusion-plugins-ecosystem/product-page
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: product-page
title: {zh: "Fusion-Plugins-Ecosystem 产品页", en: "Fusion-Plugins-Ecosystem Product Page"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - fusion_plugins_ecosystem/
---

# Fusion-Plugins-Ecosystem

插件注册中心、生命周期管理器、MCP 服务与 Claude 全链路适配层，构建在 fusion-cowork 之上。它不是独立运行时 —— 进程托管、MCP 网关、硬件调度、日志收集均由 fusion-cowork 提供；本包只做注册、生命周期、Claude 适配、连接器与市场。

Plugin registry, lifecycle manager, MCP server, and native Claude full-chain adaptation layer built on fusion-cowork. It is not a standalone runtime — process hosting, MCP gateway, hardware scheduling, and log collection are provided by fusion-cowork; this package adds registry, lifecycle, Claude adaptation, connectors, and marketplace.

## 平台信息 / Platform

| 字段 / Field | 值 / Value |
|---|---|
| serves | fusion-cowork 之上的插件生态层：注册、生命周期、Claude 全链路适配、外部 MCP 连接器、市场 / Plugin ecosystem layer on top of fusion-cowork: registry, lifecycle, Claude adaptation, external MCP connectors, marketplace |
| builds_on | fusion-cowork（运行时/网关/硬件/会话）、fusion-core（config 单点）、fusion-mlx（经 DeskRuntime 的本地推理）/ fusion-cowork (runtime, gateway, hardware, sessions), fusion-core (config single point), fusion-mlx (local inference via DeskRuntime) |
| hosts | `fusion-plugin-server`（MCP stdio/SSE/HTTP，端口 8765）、`fusion-plugin-console`（FastAPI Extensions 控制台，端口 8795）/ `fusion-plugin-server` (MCP stdio/SSE/HTTP, port 8765), `fusion-plugin-console` (FastAPI extensions console, port 8795) |
| integration_surface | MCP 2026-07-28 协议（stdio/SSE/HTTP）、Claude Skill/Agent/Plugin Bundle 导出、fusion-gateway shell 模块注册（侧边栏 `/shell/fusion-plugins-ecosystem/`）、fusion-security 审计 JSONL 管道 / MCP 2026-07-28 protocol (stdio/SSE/HTTP), Claude Skill/Agent/Plugin Bundle export, fusion-gateway shell module registration (sidebar `/shell/fusion-plugins-ecosystem/`), fusion-security audit JSONL pipeline |
| extension_points | `PluginManifest` 声明式插件、connector pack（声明式 JSON）、REST/webhook 连接器文档、marketplace 索引（本地 JSON 或 HTTPS）/ declarative `PluginManifest` plugins, connector packs (declarative JSON), REST/webhook connector docs, marketplace index (local JSON or HTTPS) |

## 核心能力 / Capabilities

### 1. 插件注册 / Plugin registry

**中文**：声明式 `PluginManifest`（id、capabilities、params schema、entry_point、timeout、sandbox_mode、depends_on）。`registry.register_builtin()` 一次调用注册内置 caveman 压缩插件。消费者不再需要手写：工具目录维护、参数 schema 到 MCP inputSchema 的映射、插件分类与检索。约束：manifest 冻结（frozen dataclass），注册后不可变；id 全局唯一。

**EN**: Declarative `PluginManifest` (id, capabilities, params schema, entry_point, timeout, sandbox_mode, depends_on). `registry.register_builtin()` registers the built-in caveman compressor in one call. Consumers no longer write: tool-catalog maintenance, params-to-MCP-inputSchema mapping, plugin categorization and lookup. Constraint: manifests are frozen dataclasses, immutable after registration; ids are globally unique.

### 2. 生命周期 / Lifecycle

**中文**：load/enable/disable/execute、超时熔断（meltdown）、自动重启（全局 `max_auto_restart`，插件级 `max_restart` 可覆盖）、INLINE/PROCESS 两种沙箱模式、`max_concurrent_plugins`（默认 16）并发闸。消费者不再需要手写：插件加载顺序（`depends_on` 拓扑序）、超时与熔断状态机、重启退避。约束：PROCESS 模式经子进程 IPC 隔离，心跳超时 120 秒判定僵死。

**EN**: load/enable/disable/execute, timeout meltdown, auto-restart (global `max_auto_restart`, per-plugin `max_restart` override), INLINE/PROCESS sandbox modes, and a `max_concurrent_plugins` (default 16) concurrency gate. Consumers no longer write: load ordering via `depends_on` topological sort, timeout/meltdown state machines, restart backoff. Constraint: PROCESS mode isolates via subprocess IPC; heartbeat stale after 120 seconds marks the plugin dead.

### 3. Claude 全链路适配 / Claude full-chain adaptation

**中文**：声明 `mcp_tool`/`claude_skill`/`subagent` 能力后自动暴露 —— Skill bundle（`skill_adapter.py`）、Agent .md（`agent_adapter.py`）、Plugin Bundle（`plugin_bundle.py`）、MCP Tool（`mcp_exporter.py`），统一经 `claude_gateway.py`。正向：Claude 调用本地能力；反向：`dispatch_subagent` 拉起 Claude Code 子代理。消费者不再需要手写：任何 per-plugin Claude 适配代码（零适配）。约束：适配形态由能力声明驱动，未声明的能力不暴露；MCP 协议版本固定 2026-07-28。

**EN**: Declaring `mcp_tool`/`claude_skill`/`subagent` capabilities auto-exposes the plugin — Skill bundle (`skill_adapter.py`), Agent .md (`agent_adapter.py`), Plugin Bundle (`plugin_bundle.py`), MCP Tool (`mcp_exporter.py`), all through `claude_gateway.py`. Forward: Claude calls local capabilities. Reverse: `dispatch_subagent` drives a Claude Code subagent. Consumers no longer write: any per-plugin Claude adapter code (zero adapter). Constraint: exposure is driven by declared capabilities only; MCP protocol version is pinned to 2026-07-28.

### 4. 外部 MCP 连接器 / External MCP connectors

**中文**：fusion 作为 MCP 客户端消费任意外部 MCP server。第一方 pack：Feishu（官方 lark-mcp）、DingTalk、WeCom、Mail（IMAP/SMTP）、Calendar（CalDAV）、Notion（官方 @notionhq/notion-mcp-server）。pack 是纯数据（transport/command/url + 凭证 provider + 工具风险级）。消费者不再需要手写：MCP 客户端连线、凭证注入（stdio→env、HTTP→Authorization）、HIGH_RISK 审批闸。约束：连接器工具只跑 INLINE；`sandbox_default_mode=process` 时拒绝安装；HIGH 风险工具未 `approve_tool()` 前抛 `PermissionError`；凭证只存本地（Desk Fernet 或本地加密文件）。

**EN**: fusion acts as an MCP client consuming arbitrary external MCP servers. First-party packs: Feishu (official lark-mcp), DingTalk, WeCom, Mail (IMAP/SMTP), Calendar (CalDAV), Notion (official @notionhq/notion-mcp-server). A pack is pure data (transport/command/url + credential providers + tool risk levels). Consumers no longer write: MCP client wiring, credential injection (stdio→env, HTTP→Authorization), HIGH_RISK approval gates. Constraints: connector tools run INLINE only; installation is refused when `sandbox_default_mode=process`; HIGH-risk tools raise `PermissionError` until `approve_tool()`; credentials stay local (Desk Fernet store or local encrypted file).

### 5. Extensions 控制台 / Extensions console

**中文**：`fusion-plugin-console`（FastAPI，127.0.0.1:8795）四标签页：Connectors、Plugins·MCP、Skills、Sub-agents。声明式 REST/webhook 连接器（READ 通道 GET 自动桥接为 `connector__<id>__<op>`；WRITE 通道进入人工审核队列，永不自动暴露）；声明式 plugin.json 安装（无任意代码）；远端 MCP server 探测注册（`mcp__<server>__<tool>`）；能力脊线从磁盘事实派生，重启 `resync()` 重建。消费者不再需要手写：连接器管理 UI、安装流、能力清单页。约束：控制台默认只绑 127.0.0.1；`/api/*` 在存在密钥时要求 Bearer/x-api-key。

**EN**: `fusion-plugin-console` (FastAPI, 127.0.0.1:8795) hosts four tabs: Connectors, Plugins·MCP, Skills, Sub-agents. Declarative REST/webhook connectors (READ channels, GET, bridge as `connector__<id>__<op>`; WRITE channels enter a human review queue and are never auto-listed); declarative plugin.json install (no arbitrary code); remote MCP server probe and register (`mcp__<server>__<tool>`); the capability spine derives from disk facts and is rebuilt via `resync()` at boot. Consumers no longer write: connector management UI, install flows, capability catalog pages. Constraints: the console binds 127.0.0.1 by default; `/api/*` requires Bearer/x-api-key when a key exists.

### 6. 计量与观测 / Metering and observability

**中文**：`token_meter.py` 拆分 Claude 模型消耗与插件本地计算，WAL append 写持久化（上限 `max_token_records` 10000 条）；`metrics.py` 提供 Prometheus 计数器/仪表与 `/metrics` 端点；`logging_setup.py` 结构化 JSON 日志 + correlation_id。消费者不再需要手写：token 台账、指标埋点、日志关联。约束：token 记录超上限后滚动淘汰；meter 持久化路径为 None 时纯内存。

**EN**: `token_meter.py` splits Claude model consumption from plugin local compute, persists via WAL append-write (capped at `max_token_records`, 10000); `metrics.py` exposes Prometheus counters/gauges and a `/metrics` endpoint; `logging_setup.py` emits structured JSON logs with correlation_id. Consumers no longer write: token ledgers, metric instrumentation, log correlation. Constraints: token records evict on overflow; a None meter persist path means in-memory only.

## 边界 / Non-goals

**中文**：不做进程托管、权限内核、MCP 网关、硬件调度 —— 这些属于 fusion-cowork。不做推理 —— 属于 fusion-mlx。不做租户/认证 —— 属于 fusion-identity。

**EN**: No process hosting, permission kernel, MCP gateway, or hardware scheduling — those belong to fusion-cowork. No inference — fusion-mlx. No tenancy/auth — fusion-identity.

## 快速开始 / Get started

See [get-started.md](get-started.md) and the recipes under [docs/recipes/](recipes/).
