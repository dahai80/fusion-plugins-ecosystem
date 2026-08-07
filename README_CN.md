<div align="center">
  <h1>🔌 Fusion-Plugins-Ecosystem</h1>
  <p><strong>基于 fusion-desk 的插件注册中心、生命周期管理器、Claude 全链路适配层。</strong></p>
  <p><em>构建于 fusion-desk 之上。所有插件自动暴露给 Claude。零适配代码。</em></p>
  <p><a href="README.md">English</a> | 中文</p>
</div>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-brightgreen" alt="macOS">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/base-fusion--desk-orange" alt="fusion-desk">
  <img src="https://img.shields.io/badge/Claude-native-blueviolet" alt="Claude">
  <img src="https://img.shields.io/badge/MCP-2026--07--28-yellow" alt="MCP">
  <img src="https://img.shields.io/badge/tests-411%20passed-success" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.3.1-blue" alt="Version">
  <img src="https://img.shields.io/badge/coverage-99%25-success" alt="Coverage">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
</p>

---

# Fusion-Plugins-Ecosystem

> 基于 `fusion-desk` 的插件注册中心、生命周期管理器、Claude 全链路适配层。所有插件自动暴露为 Claude Skill 和 MCP Tool，无需单独写适配层。

## 📋 概览

`fusion-plugins-ecosystem` 是 `fusion-desk` 的**上层子模块**，**不是**独立项目——进程托管、权限控制、日志采集、资源限流全部由 `fusion-desk` 统一提供。本包只负责：

1. **插件注册中心**：声明式清单、参数 schema、能力声明
2. **生命周期管理**：加载/卸载/启用/禁用/热重载，超时熔断，进程自动重启，INLINE/PROCESS 沙箱双模
3. **Claude 全链路适配**：插件自动转 Claude Code Skill/Agent/Plugin Bundle，暴露为 MCP Tools（2026-07-28）
4. **Token 统一计量**：区分 Claude 模型消耗 vs 插件本地计算开销，JSON 持久化
5. **MCP 协议栈**：stdio/SSE/HTTP 传输层，JSON-RPC 2.0 处理器，MCP Server CLI
6. **插件沙箱**：子进程 IPC 进程隔离，资源限制，心跳监控
7. **Desk 上下文桥**：复用 Desk 的 MCP 网关、硬件调度器、会话池

### 架构分层

```
fusion-plugin-ecosystem        ← 本包：注册、生命周期、Claude 适配、沙箱、MCP Server
        ↓ 依赖 API
fusion-desk runtime            ← 底座：MCP 网关、硬件、会话、日志
        ↓
fusion-mlx core                ← Mac 本地推理内核（Metal/MLX）
```

### Claude 三层兼容

| 接入方式 | 常量 | 网关入口 |
|---------|------|---------|
| Claude Desktop 客户端 | `CLAUDE_DESKTOP` | `list_mcp_tools` + `gateway_info`（stdio transport） |
| VS Code Claude Code 插件 | `CLAUDE_CODE` | `dispatch_subagent` + `list_subagent_capable_plugins` |
| 网页版 Claude | `CLAUDE_WEB` | `invoke_mcp_tool`（HTTP/SSE 中继） |
| 火山方舟 Claude Coding Plan | `CLAUDE_VOLCENGINE` | `store_credentials("volcengine_claude", ...)` |

**双向互通**：
- **正向**：Claude 调用 fusion 全部本地能力（图片视频生成、MLX 本地推理、文件操作、量化工具）
- **反向**：fusion-desk 主动拉起 Claude Code 子代理，完成项目批量重构、PR 生成、代码优化

完整规范见 [docs/CLAUDE_COMPATIBILITY.md](docs/CLAUDE_COMPATIBILITY.md)。

## 🚀 快速开始

### 安装

```bash
# 克隆
git clone https://github.com/dahai80/fusion-plugins-ecosystem.git
cd fusion-plugins-ecosystem

# 创建 venv（规避 PEP 668）
python3 -m venv .venv
.venv/bin/pip install -e ../fusion-desk      # 底座 runtime（非 PyPI 包）
.venv/bin/pip install -e ".[test]"

# 运行测试
.venv/bin/python -m pytest --cov=fusion_plugins_ecosystem --cov-report=term-missing -q
# → 407 passed
```

### 最小用法

```python
import fusion_plugins_ecosystem as fpe

# 注册中心 + 内置 caveman 压缩
registry = fpe.PluginRegistry()
registry.register_builtin()

# Claude 全链路网关
lifecycle = fpe.PluginLifecycle(registry)
gw = fpe.ClaudeGateway(registry, lifecycle)

# 正向：暴露给 Claude
skills = gw.export_skills()           # → Claude Skill 字典
tools = gw.list_mcp_tools()           # → MCP 工具描述

# 正向：Claude 通过 MCP 调用
resp = await gw.invoke_mcp_tool(
    "caveman_compress", {"text": "# 注释\ncode"}
)
# → {"content": [{"type":"text","text":"{\"compressed\":\"code\",...}"}], "isError": false}

# 反向：拉起 Claude Code 子代理
task = fpe.SubagentTask(
    name="compress-session",
    plugin_id="caveman_compress",
    arguments={"text": "a\n\n\n\nb"},
)
result = await gw.dispatch_subagent(task)
# → {"state": "completed", "result": {"compressed": "a\n\nb", ...}}
```

## 🧩 编写插件

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
        PluginCapability.MCP_TOOL,        # 自动暴露为 MCP Tool
        PluginCapability.CLAUDE_SKILL,    # 自动转 Claude Skill
    ],
    params=[
        PluginParam(name="text", type="string", description="Input", required=True),
    ],
    entry_point=my_plugin,
    timeout_seconds=60,
)
```

注册一次 → 自动出现在 Claude 工具目录和 MCP `tools/list` 中，无需适配代码。详见 [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md)。

## 📚 文档

| 文档 | 内容 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层、模块图、数据流（正向/反向）、能力映射、痛点解决 |
| [docs/API.md](docs/API.md) | 完整公开 API 参考：`PluginRegistry`、`PluginLifecycle`、`ClaudeGateway`、`TokenMeter`、`DeskContext`、`EcosystemConfig` |
| [docs/CLAUDE_COMPATIBILITY.md](docs/CLAUDE_COMPATIBILITY.md) | 三层兼容、正向/反向流、火山方舟鉴权、MLX 视觉后端、token 计量、配置开关 |
| [docs/PLUGIN_DEVELOPMENT.md](docs/PLUGIN_DEVELOPMENT.md) | 插件开发指南：清单、能力、参数 schema、异步、惰性入口、测试清单 |

## 🗂️ 项目结构

```
fusion-plugins-ecosystem/
├── pyproject.toml
├── LICENSE                       ← Apache 2.0
├── README.md                     ← 英文版
├── README_CN.md                  ← 中文版（本文件）
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API.md
│   ├── CLAUDE_COMPATIBILITY.md
│   └── PLUGIN_DEVELOPMENT.md
├── fusion_plugins_ecosystem/
│   ├── __init__.py               ← 顶层导出 + 惰性导入
│   ├── desk_runtime.py           ← fusion-desk 运行时句柄包装
│   ├── desk_context.py           ← DeskContext 薄包装委托层
│   ├── registry.py               ← 插件注册中心 + 冻结清单
│   ├── lifecycle.py              ← 加载/启用/执行 + 熔断 + 重启 + INLINE/PROCESS
│   ├── sandbox.py                ← 插件沙箱（进程隔离、IPC）
│   ├── transport.py              ← MCP 传输层（stdio/SSE/HTTP）
│   ├── jsonrpc.py                ← MCP JSON-RPC 2.0 处理器
│   ├── server.py                 ← MCP Server 入口 + CLI
│   ├── schema.py                 ← 共享 schema（SandboxMode、MCPAnnotations、类型）
│   ├── skill_adapter.py          ← 插件 → Claude Code Skill bundle
│   ├── agent_adapter.py          ← 插件 → Claude Code Agent .md
│   ├── plugin_bundle.py          ← Claude Code Plugin bundle 生成器
│   ├── claude_adapter.py         ← 旧版适配器（向后兼容）
│   ├── mcp_exporter.py           ← 插件 → MCP Tools
│   ├── claude_gateway.py         ← 统一 Claude 全链路网关
│   ├── token_meter.py            ← 统一 token 计量 + 持久化
│   ├── config.py                 ← 一键配置面板 + 观察者
│   ├── hook_adapter.py           ← Claude Code Plugin hooks 适配器
│   └── builtin/
│       ├── __init__.py
│       └── caveman_compress.py   ← 内置 token 压缩器
└── tests/                        ← 411 测试
    ├── test_caveman.py
    ├── test_claude_adapter.py
    ├── test_claude_gateway.py
    ├── test_config.py
    ├── test_desk_context.py
    ├── test_desk_runtime.py
    ├── test_lifecycle.py
    ├── test_mcp_exporter.py
    ├── test_registry.py
    ├── test_registry_full.py
    ├── test_token_meter.py
    ├── test_jsonrpc.py
    ├── test_transport_server.py
    ├── test_sandbox.py
    ├── test_hook_adapter.py
    ├── test_phase3_adapters.py
    ├── test_phase4_meter_config.py
    ├── test_schema.py
    └── test_integration.py
```

## 🔧 配置面板（一键开关）

`EcosystemConfig` — 所有开关默认 `True`（原生完整兼容 Claude）。

| 开关 | 控制 | 关闭行为 |
|------|------|---------|
| `enable_claude_mcp` | `list_mcp_tools`、`invoke_mcp_tool` | 返回 `[]` / `isError=True` |
| `auto_export_claude_skill` | `export_skills` | 返回 `[]` |
| `default_mount_compressor` | `export_default_mounted_skills` | 返回 `[]` |
| `subagent_timeout_destroy` | `dispatch_subagent` | 超时不 unload |
| `enable_volcengine_claude_plan` | 火山方舟密钥存/取 | 拒绝 / `None` |
| `enable_mixed_quantization` | `mlx_visual_backend` | 抛 `RuntimeError` |
| `mcp_transport` | MCP 传输类型 | `stdio` / `sse` / `http` |
| `sandbox_default_mode` | 默认沙箱模式 | `inline` / `process` |
| `max_token_records` | 最大 token 记录数 | 剪枝阈值 |
| `token_persist_path` | Token 持久化文件 | `None` = 内存模式 |

```python
from fusion_plugins_ecosystem import EcosystemConfig

# 默认全开
config = EcosystemConfig()
# 关闭 MCP 暴露
config.enable_claude_mcp = False
# 通过 Desk 配置中心持久化
d = config.to_dict()
restored = EcosystemConfig.from_dict(d)
```

## 🛠️ 痛点解决

| 痛点（来自 PRD） | 解决方式 |
|------------------|---------|
| 子代理跑 40 分钟无 token 消耗、卡死无日志 | `TokenMeter` 在 `wall>60 ∧ total=0` 告警；所有日志经 `DeskContext.log` |
| 显存抢占冲突 | `DeskRuntime.acquire_vram` 强制 `vram_total_mb` 预算台账 |
| MCP 端口冲突 | fusion-desk 单一 MCP 网关；`MCPExporter` multiplex |
| 子代理卡死无统一重启 | `PluginLifecycle` 超时熔断 + `_maybe_restart`（≤ `MAX_RESTART`） |
| 心跳卡死检测 | `PluginLifecycle._watch_loop` 标记 `HEARTBEAT_STALE` → `TIMEOUT` |

## 🧪 测试

```bash
.venv/bin/python -m pytest --cov=fusion_plugins_ecosystem --cov-report=term-missing -q
```

最新结果：**411 passed**。

| 测试文件 | 用例数 | 覆盖范围 |
|---------|-------|---------|
| `test_desk_runtime.py` | 30 | vRAM / 日志 / 权限 / API 密钥 / MLX / 节点桥 / 调度器桥 |
| `test_desk_context.py` | 18 | 薄包装委托 |
| `test_lifecycle.py` | 24 | 加载/启用/禁用/卸载/执行/超时/崩溃/重启/看门狗 |
| `test_mcp_exporter.py` | 13 | list_tools / call_tool / gateway_info / manifest_to_mcp_tool |
| `test_token_meter.py` | 16 | TokenRecord / record / measure / summary / 卡死子代理告警 |
| `test_claude_adapter.py` | 13 | export_one / export_all / export_default_mounted / 参数类型 / 枚举 / 必填 |
| `test_claude_gateway.py` | 45 | 三层兼容 / 正向反向 / 鉴权 / 配置开关 / token 集成 |
| `test_config.py` | 9 | 默认值 / to_dict / from_dict / 往返 / 部分 / 未知键 |
| `test_registry_full.py` | 15 | 注册 / 注销 / 列表 / 分类 / 默认挂载 / 枚举值 |
| `test_caveman.py` | 22 | _compress_text / caveman_compress / CAVEMAN_MANIFEST 字段 |
| `test_registry.py` | 13 | （旧版）注册 + 适配器 + 导出器 + caveman 集成 |
| `test_hook_adapter.py` | 8 | HookAdapter 事件映射 / 能力过滤 |

## ⚠️ 技术约束

- **不能脱离 fusion-desk 独立运行**——进程托管、权限、日志、资源全部依赖 Desk
- **Python ≥ 3.11**——与 fusion-desk 对齐
- **Mac M 系列原生**——依赖 fusion-mlx Metal/MLX 加速
- **禁止直接 import `fusion_desk` 内部**——一律经 `DeskContext`

## 📄 License

Apache License 2.0 — [Fusion-MLX](https://github.com/fusion-mlx) Apple Silicon 本地 AI 生态的一部分。
