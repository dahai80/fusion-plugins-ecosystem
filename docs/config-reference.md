---
doc_id: fusion-plugins-ecosystem/config-reference
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: config-reference
title: {zh: "配置参考", en: "Config Reference"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - fusion_plugins_ecosystem/config.py
  - fusion_plugins_ecosystem/server.py
  - fusion_plugins_ecosystem/console_app.py
  - fusion_plugins_ecosystem/shell_registry.py
---

# 配置参考 / Config Reference

## EcosystemConfig（config.py）

数据类，默认全部"原生兼容 Claude"开启；经 `to_dict()`/`from_dict()` 持久化到 Desk 配置中心，支持 observer 回调。

Dataclass defaulting to all Claude-compat toggles on; persisted via `to_dict()`/`from_dict()` to the Desk config center, with observer callbacks.

### Claude 全链路 / Claude full-chain

| 字段 / Field | 类型 / Type | 默认 / Default | 说明 / Notes |
|---|---|---|---|
| enable_claude_mcp | bool | True | 启用 MCP 对外暴露 / expose via MCP |
| auto_export_claude_skill | bool | True | 自动导出 Skill / auto-export skills |
| subagent_timeout_destroy | bool | True | 子代理超时自动销毁 / destroy timed-out subagents |
| default_mount_compressor | bool | True | 默认挂载 caveman 压缩插件 / mount compressor by default |
| enable_volcengine_claude_plan | bool | True | 火山方舟 Coding Plan 鉴权 / Volcengine Coding Plan auth |

### 痛点配套 / Companion toggles

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| unified_log_to_desk | bool | True | 插件日志汇总到 Desk / logs to Desk |
| enable_mixed_quantization | bool | True | MLX 本地推理作视觉后端 / MLX as visual backend |

### 超时与重启 / Timeouts and restarts

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| subagent_timeout_seconds | int | 600 | 子代理超时 / subagent timeout |
| heartbeat_stale_seconds | int | 120 | 心跳僵死阈值 / stale-heartbeat threshold |
| max_auto_restart | int | 3 | 最大自动重启次数 / max auto-restarts |

### MCP 服务 / MCP server

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| mcp_transport | str | "stdio" | stdio/sse/http |
| mcp_host | str | "127.0.0.1" | 监听地址 / bind host |
| mcp_port | int | 0 | 0 = auto |

### 沙箱与执行 / Sandbox and execution

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| sandbox_default_mode | str | "inline" | `process` 时拒绝连接器安装 / process blocks connector install |
| log_level | str | "INFO" | DEBUG/INFO/WARNING/ERROR |
| vram_limit_mb | int | 0 | 0 = 不限制 / no limit；启动同步到 DeskRuntime / synced to DeskRuntime at boot |
| max_concurrent_plugins | int | 16 | inline 线程池并发闸 / inline concurrency gate |

### 计量 / Metering

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| max_token_records | int | 10000 | token 记录上限 / record cap |
| token_persist_path | str \| None | None | None = 纯内存 / in-memory only |

### 连接器框架 / Connector framework

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| enable_connector_framework | bool | True | 连接器安装/桥接总开关 / master switch |
| connector_persist_path | str \| None | None | None = 纯内存 / in-memory only |
| connector_marketplace_index | str \| None | None | 本地 JSON 或 HTTPS URL / local JSON or HTTPS URL |

## 环境变量 / Environment variables

全部经 `fusion_core.config` 单点解析（issue #38），不直接 `os.environ`。

All resolved via the `fusion_core.config` single point (issue #38), never raw `os.environ`.

| 变量 / Variable | 作用 / Effect | 来源 / Source |
|---|---|---|
| `FUSION_ADMIN_KEY` | Extensions 控制台向 gateway 注册 shell 模块；缺失时 dev 仅告警、注册 OFF，prod `RuntimeError` 快速失败 / console shell registration; dev warns and stays OFF, prod fails fast | `shell_registry.py` |
| `FUSION_GATEWAY_BASE_URL` | gateway 地址，默认 `http://127.0.0.1:11432` / gateway address | `shell_registry.py` |
| `FUSION_PLUGINS_ECOSYSTEM_DATA_DIR` | 数据目录，默认 `~/.fusion-plugins-ecosystem` / data dir | `config.py` |
| `FUSION_PLUGINS_ECOSYSTEM_PORT` | 固定 MCP server 端口（port-registry: 8765）/ pin MCP server port | `server.py` |
| `FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT` | 固定控制台端口 / pin console port | `console_app.py` |

## 端口 / Ports

- MCP server：8765（port-registry 注册；默认 0 = auto）
- Extensions 控制台：8795（`console_app.py` `DEFAULT_PORT`）
- fusion-gateway：11432（上游依赖，`fusion_core.config.gateway_base_url()`）

- MCP server: 8765 (port-registry; default 0 = auto)
- Extensions console: 8795 (`DEFAULT_PORT` in `console_app.py`)
- fusion-gateway: 11432 (upstream dependency)

## 数据目录文件 / Data-dir files

`DATA_DIR`（默认 `~/.fusion-plugins-ecosystem`，可被 env 覆盖，不迁移既有部署）下的已知产物 / known artifacts under the data dir:

| 路径 / Path | 生成者 / Producer | 说明 / Notes |
|---|---|---|
| `web_module_key` | `shell_registry.py` | gateway 签发的 fm- key，权限 0600 / gateway-issued key, chmod 0600 |
| `vault.json` | Extensions 控制台 | Desk Fernet 不可用时的本地凭证兜底，chmod 600 / local credential fallback |
| `connectors.json` | `ConnectorRegistry` | 连接器持久化（路径可配）/ connector persistence |
