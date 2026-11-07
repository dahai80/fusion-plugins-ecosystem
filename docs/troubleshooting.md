---
doc_id: fusion-plugins-ecosystem/troubleshooting
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: troubleshooting
title: {zh: "故障排查", en: "Troubleshooting"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - docs/PLUGIN_DEVELOPMENT.md
  - fusion_plugins_ecosystem/shell_registry.py
---

# 故障排查 / Troubleshooting

## 安装与导入 / Install and import

### `ModuleNotFoundError: fusion_cowork` / `fusion_core`

fusion-cowork 与 fusion-core 不在 PyPI。先装基础运行时，再装本包（共享 venv 中 fusion-core 通常已就位）：

fusion-cowork and fusion-core are not on PyPI. Install the base runtime first:

```bash
source /Users/dahai/fusion/.venv/bin/activate
pip install -e "../fusion-cowork"
pip install -e ".[test]"
```

### 控制台启动报缺 fastapi/uvicorn

`fusion-plugin-console` 依赖 console extra：

```bash
pip install -e ".[console]"
```

## 运行时 / Runtime

### HIGH 风险工具执行抛 `PermissionError`

设计行为：外部连接器 HIGH_RISK 工具必须先显式审批。执行：

```python
creg.approve_tool("feishu", "im.message.send")
```

只读工具按约定为 LOW，无需审批（测试强制此约定）。/ Read-only tools are LOW by convention (test-enforced) and need no approval.

### 连接器 pack 安装被拒绝 / Connector pack install refused

`EcosystemConfig.sandbox_default_mode = "process"` 时连接器安装被阻止 —— 连接器工具只允许 INLINE（外部 MCP server 本身已是隔离进程）。改回 `inline` 或在安装前调整配置。

Connector install is blocked when `sandbox_default_mode="process"` — connector tools run INLINE only. Reset the toggle or adjust config before installing.

### 插件超时后连续重启 / Plugin times out and restarts repeatedly

生命周期含超时熔断与自动重启（默认 `max_auto_restart=3`，插件级 `max_restart` 可覆盖）。若插件持续超时：

- 用 `PluginCapability.LONG_TASK` 声明长任务并调大 `timeout_seconds`；
- 检查 `max_concurrent_plugins`（默认 16）是否打满导致排队。

Lifecycle has meltdown and auto-restart (global `max_auto_restart=3`, per-plugin `max_restart` override). For a plugin that keeps timing out: declare `LONG_TASK` and raise `timeout_seconds`, and check whether the `max_concurrent_plugins` gate (16) is saturated.

### PROCESS 沙箱插件僵死 / PROCESS plugin appears dead

心跳超过 `heartbeat_stale_seconds`（默认 120 秒）判定僵死并按重启策略处理。用 `desk.get_logs(...)` 查看插件日志；PROCESS 模式经子进程 IPC，先确认子进程未被外部 kill。

A stale heartbeat beyond `heartbeat_stale_seconds` (120 s) marks the plugin dead and triggers the restart policy. Check plugin logs via `desk.get_logs(...)`; PROCESS mode runs via subprocess IPC — confirm the child was not killed externally.

### MLX 相关插件失败 / MLX-backed plugin fails

本包无 `start.sh`，不托管推理。先启动推理引擎，再走 `DeskRuntime.mlx_chat`：

```bash
/Users/dahai/fusion/fusion-mlx/start.sh start
/Users/dahai/fusion/fusion-mlx/start.sh status
```

## Extensions 控制台 / Extensions console

### 侧边栏没有出现本模块 / Shell module not registered

shell 注册以 `FUSION_ADMIN_KEY` 能否解析为闸（issue #38）：dev 环境缺 key 只告警、注册保持 OFF，属预期行为；prod 环境缺 key 直接 `RuntimeError`。设置 key 后重启控制台；gateway 地址默认 `http://127.0.0.1:11432`，可用 `FUSION_GATEWAY_BASE_URL` 覆盖。

Shell registration is gated on `FUSION_ADMIN_KEY` resolution: in dev a missing key warns and stays OFF (expected); in prod it raises `RuntimeError`. Set the key and restart the console; gateway address defaults to `http://127.0.0.1:11432`, override with `FUSION_GATEWAY_BASE_URL`.

### `/api/*` 返回 401

存在密钥（静态 env key 或 gateway 签发的 fm- key）时控制台 API 要求 `Authorization: Bearer` 或 `x-api-key` 头。fm- key 持久化在 `~/.fusion-plugins-ecosystem/web_module_key`（0600）。

When any key exists (static env key or gateway-issued fm- key), console APIs require a `Authorization: Bearer` or `x-api-key` header. The fm- key persists at `~/.fusion-plugins-ecosystem/web_module_key` (0600).

### 凭证保存失败 / Credential vault failures

优先用 Desk Fernet 加密存储；Desk config_center 缺失时回退控制台本地 `vault.json`（chmod 600）。若两者都不可用，安装流程会在校验阶段报错 —— 先修复数据目录权限（`FUSION_PLUGINS_ECOSYSTEM_DATA_DIR`，默认 `~/.fusion-plugins-ecosystem`）。

Desk Fernet store is preferred; without Desk config_center the console falls back to a local `vault.json` (chmod 600). If both are unavailable the install flow fails at validation — fix data-dir permissions first.

## 市场 / Marketplace

### 安装市场条目失败 / Marketplace install fails

- 远程索引必须 HTTPS（测试强制 HTTPS-only 约束）；本地索引用 JSON 文件路径。
- 索引条目 id 与内置目录冲突时，索引覆盖内置（预期行为）。
- `skill_package` 条目从 git/local source 安装到本地目录，先确认 source 可达。

- Remote indexes must be HTTPS (test-enforced); local indexes use a JSON file path.
- Index entries override builtin catalog entries on id collision (by design).
- `skill_package` entries install from a git/local source into the local install dir — verify the source is reachable.

## 诊断工具 / Diagnostics

```bash
pytest tests/test_lifecycle.py -v              # lifecycle behavior
fusion-plugin-server --transport stdio --log-level DEBUG   # MCP wire protocol
fusion-plugin-server --transport http --port 8765          # then probe /metrics and tools/list
```

结构化 JSON 日志带 correlation_id（`logging_setup.py`）；Prometheus 指标经 `metrics.py` 暴露，可用于确认计数与仪表状态。/ Structured JSON logs carry correlation_id; Prometheus metrics from `metrics.py` confirm counter/gauge state.
