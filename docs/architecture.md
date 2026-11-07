---
doc_id: fusion-plugins-ecosystem/architecture
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: architecture
title: {zh: "架构", en: "Architecture"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - fusion_plugins_ecosystem/__init__.py
  - docs/PLUGIN_DEVELOPMENT.md
---

# 架构 / Architecture

## 分层 / Layering

```
fusion-plugins-ecosystem   ← 本包：注册、生命周期、Claude 适配、沙箱、MCP server
        ↓ depends on API
fusion-cowork runtime      ← 基础运行时：MCP 网关、硬件调度、会话、日志
        ↓
fusion-mlx core            ← Mac 本地推理内核（Metal/MLX）
```

本包是 fusion-cowork 的上层模块，不是独立项目：进程托管、权限控制、日志汇总、资源节流都由 cowork 提供。fusion-core 提供 config 单点（端口/数据目录/admin key 解析，issue #38）。

This package is an upper-layer module of fusion-cowork, not a standalone project: process hosting, permission control, log collection, and resource throttling come from cowork. fusion-core provides the config single point (port/data-dir/admin-key resolution, issue #38).

## 模块地图 / Module map

| 模块 / Module | 职责 / Responsibility |
|---|---|
| `registry.py` | 插件注册表 + 冻结 manifest（PluginManifest/PluginParam/PluginCapability/PluginCategory）/ registry + frozen manifests |
| `lifecycle.py` | load/enable/execute + 熔断 + 自动重启 + INLINE/PROCESS / load/enable/execute, meltdown, restart, sandbox modes |
| `sandbox.py` | 插件沙箱：子进程隔离、IPC、心跳 / subprocess isolation, IPC, heartbeat |
| `desk_runtime.py` | fusion-cowork 运行时句柄：日志、VRAM、文件权限、凭证、mlx_chat / cowork runtime handle |
| `transport.py` + `jsonrpc.py` + `server.py` | MCP 传输（stdio/SSE/HTTP）+ JSON-RPC 2.0 handler + CLI / MCP transports + JSON-RPC handler + CLI |
| `claude_gateway.py` | 统一 Claude 全链路网关 / unified Claude full-chain gateway |
| `skill_adapter.py` / `agent_adapter.py` / `plugin_bundle.py` / `mcp_exporter.py` | 插件 → Skill/Agent/Bundle/MCP Tool 导出 / plugin export adapters |
| `token_meter.py` | Claude 消耗 vs 本地计算拆分计量，WAL 持久化 / split token metering, WAL persistence |
| `metrics.py` / `logging_setup.py` | Prometheus 指标、结构化 JSON 日志 + correlation_id / metrics, structured logs |
| `connector.py` + `connector_packs.py` | 外部 MCP 连接器框架 + 第一方 pack（Feishu/DingTalk/WeCom/Mail/Calendar/Notion）/ connector framework + first-party packs |
| `rest_connector.py` | 声明式 REST/webhook 连接器文档 / declarative REST connector docs |
| `marketplace.py` | 连接器/skill 包市场（内置目录 + JSON 索引）/ marketplace (builtin catalog + JSON index) |
| `extensions_console.py` + `console_app.py` + `shell_registry.py` | 控制台后端 + FastAPI 应用（8795）+ gateway shell 注册 / console backend + app + shell registration |
| `config.py` | EcosystemConfig 一键开关面板 / one-toggle config panel |
| `audit_events.py` | 授权审计事件 → fusion-security JSONL（issue #43）/ audit events → security JSONL |
| `cluster_bridge.py` | 多节点分布式状态桥（cowork 集群同步/故障转移）/ multi-node state bridge |
| `builtin/caveman_compress.py` | 内置 token 压缩插件 / built-in compressor plugin |

## 数据流 / Data flow

### 正向（Claude → 本地能力）/ Forward (Claude → local capability)

1. 插件声明 `mcp_tool` 能力 → `mcp_exporter.py` 生成 MCP 工具描述 → `ClaudeGateway.list_mcp_tools()`。
2. Claude 经 MCP 调用 `invoke_mcp_tool(name, arguments)` → gateway 路由到 `lifecycle.execute()`。
3. INLINE：进程内线程池执行（`max_concurrent_plugins` 闸）；PROCESS：子进程 IPC + 心跳。
4. 结果包成 MCP content 返回；`token_meter` 记账；`metrics` 计数。

1. Plugin declares `mcp_tool` → `mcp_exporter.py` builds tool descriptors → `ClaudeGateway.list_mcp_tools()`.
2. Claude calls `invoke_mcp_tool(name, arguments)` → gateway routes to `lifecycle.execute()`.
3. INLINE: in-process thread pool (`max_concurrent_plugins` gate); PROCESS: subprocess IPC + heartbeat.
4. Result wrapped as MCP content; `token_meter` records usage; `metrics` counts.

### 反向（本地 → Claude Code 子代理）/ Reverse (local → Claude Code subagent)

1. 插件声明 `subagent` 能力 → 出现在 `list_subagent_capable_plugins()`。
2. `ClaudeGateway.dispatch_subagent(SubagentTask)` 提交任务，超时（默认 600 秒，`subagent_timeout_destroy=True` 时自动销毁）。
3. 结果以 `{"state": ..., "result": ...}` 返回。

1. Plugin declares `subagent` → listed in `list_subagent_capable_plugins()`.
2. `ClaudeGateway.dispatch_subagent(SubagentTask)` submits the task; on timeout (default 600 s) it is destroyed when `subagent_timeout_destroy=True`.
3. Result returns as `{"state": ..., "result": ...}`.

## Claude 三层兼容 / Three-layer compatibility

| 接入 / Access | 常量 / Constant | 网关入口 / Gateway entry |
|---|---|---|
| Claude Desktop | `CLAUDE_DESKTOP` | `list_mcp_tools` + `gateway_info`（stdio） |
| VS Code Claude Code 插件 / plugin | `CLAUDE_CODE` | `dispatch_subagent` + `list_subagent_capable_plugins` |
| Web Claude | `CLAUDE_WEB` | `invoke_mcp_tool`（HTTP/SSE 中继 / relay） |
| 火山方舟 Claude Coding Plan / Volcengine | `CLAUDE_VOLCENGINE` | `store_credentials("volcengine_claude", ...)` |

## 能力脊线 / Capability spine

Extensions 控制台的能力清单从磁盘事实（连接器文档 + MCP tick）派生，进程内注册表不持有第二事实源；启动时 `resync()` 重建。桥接 id 规则：连接器 `connector__<pack>__<tool>`、REST READ 通道 `connector__<id>__<op>`、远端 MCP `mcp__<server>__<tool>`。

The console's capability catalog derives from disk facts (connector docs + MCP ticks); the in-process registry holds no second source of truth, rebuilt by `resync()` at boot. Bridge id rules: connectors `connector__<pack>__<tool>`, REST READ channels `connector__<id>__<op>`, remote MCP `mcp__<server>__<tool>`.

## 关键约束 / Key constraints

- manifest 冻结不可变；id 全局唯一 / manifests are frozen; ids globally unique
- 连接器工具只跑 INLINE；`sandbox_default_mode=process` 拒绝连接器安装 / connector tools run INLINE only; refused when default mode is process
- 高风险工具需显式 `approve_tool()`，只读工具按约定为 LOW（测试强制）/ HIGH-risk tools need explicit approval; read-only tools are LOW by convention (test-enforced)
- 无 `start.sh`：MCP server 由 cowork 宿主或 CLI 独立启动；MLX 依赖经 `~/fusion/fusion-mlx/start.sh start` 单独启动 / no start.sh: the MCP server is hosted by cowork or started via CLI; start fusion-mlx separately
