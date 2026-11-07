---
doc_id: fusion-plugins-ecosystem/claude-compatibility
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: guide
title: {zh: "Claude 兼容性指南", en: "Claude Compatibility Guide"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - fusion_plugins_ecosystem/claude_gateway.py
  - fusion_plugins_ecosystem/config.py
---

# Claude 兼容性指南 / Claude Compatibility Guide

## 三层兼容 / Three-layer compatibility

| 接入 / Access | 常量 / Constant | 网关入口 / Gateway entry |
|---|---|---|
| Claude Desktop 客户端 / client | `CLAUDE_DESKTOP` | `list_mcp_tools` + `gateway_info`（stdio 传输 / stdio transport） |
| VS Code Claude Code 插件 / plugin | `CLAUDE_CODE` | `dispatch_subagent` + `list_subagent_capable_plugins` |
| Web Claude | `CLAUDE_WEB` | `invoke_mcp_tool`（HTTP/SSE 中继 / relay） |
| 火山方舟 Claude Coding Plan / Volcengine | `CLAUDE_VOLCENGINE` | `store_credentials("volcengine_claude", ...)` |

## 双向链路 / Bidirectional flows

**正向 / Forward** — Claude 调用全部 fusion 本地能力（MLX 本地推理、图像/视频生成、文件操作、量化工具）：插件声明能力 → 自动出现在 MCP `tools/list` 与 Skill 目录 → `invoke_mcp_tool` 经 lifecycle 执行。

Plugins declare capabilities, auto-appear in MCP `tools/list` and the Skill catalog, and run via `invoke_mcp_tool` through the lifecycle.

**反向 / Reverse** — 拉起 Claude Code 子代理：`dispatch_subagent(SubagentTask)`，默认超时 600 秒，`subagent_timeout_destroy=True` 时超时自动销毁，返回 `{"state", "result"}`。

`dispatch_subagent(SubagentTask)` drives a Claude Code subagent with a 600 s default timeout, destroyed on timeout when `subagent_timeout_destroy=True`, returning `{"state", "result"}`.

## 暴露形态 / Exposure forms

| 能力声明 / Capability | 形态 / Form | 适配器 / Adapter |
|---|---|---|
| `mcp_tool` | MCP Tool（MCP 2026-07-28） | `mcp_exporter.py` |
| `claude_skill` | Claude Code Skill bundle | `skill_adapter.py` |
| `subagent` | Claude Code Agent .md | `agent_adapter.py` |
| （组合 / bundle） | Claude Code Plugin bundle | `plugin_bundle.py` |

MCP 工具支持 `output_schema`（输出 JSON Schema）与 `mcp_annotations`（行为注解），见 `schema.py` 的 `MCP_PROTOCOL_VERSION = "2026-07-28"`。

MCP tools support `output_schema` and `mcp_annotations`; protocol version pinned in `schema.py`.

## 鉴权 / Auth

火山方舟 Claude Coding Plan：`ClaudeGateway.store_credentials("volcengine_claude", api_key)`，凭证经 Fernet 加密只存本地，`get_credentials` / `has_credentials` 读取。由 `enable_volcengine_claude_plan` 开关控制。

Volcengine Claude Coding Plan: credentials go through `store_credentials("volcengine_claude", ...)`, Fernet-encrypted and local-only, read via `get_credentials`/`has_credentials`; gated by `enable_volcengine_claude_plan`.

## 开关 / Toggles

全部经 `EcosystemConfig`（持久化到 Desk 配置中心）控制，字段与默认值见 [config-reference.md](config-reference.md)：

- `enable_claude_mcp` — MCP 对外暴露总开关 / MCP exposure master switch
- `auto_export_claude_skill` — Skill 自动导出 / auto skill export
- `default_mount_compressor` — 会话默认挂载 caveman 压缩插件 / mount compressor by default
- `subagent_timeout_destroy` / `subagent_timeout_seconds` — 子代理超时策略 / subagent timeout policy
- `enable_mixed_quantization` — MLX 本地推理作为视觉/图像生成后端 / MLX as visual backend

## Token 计量 / Token metering

`token_meter.py` 把 Claude 模型消耗与插件本地计算拆分记账，WAL append 写持久化（`token_persist_path`），上限 `max_token_records`（10000）。 / `token_meter.py` splits Claude model consumption from plugin local compute, persists via WAL append-write, capped at `max_token_records` (10000).
