---
doc_id: docs/plugin-development-cn
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: guide
title: {zh: "插件开发指南（中文）", en: "Plugin Development Guide (Chinese)"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - docs/PLUGIN_DEVELOPMENT_CN.md
  - fusion_plugins_ecosystem/registry.py
---

# 插件开发指南（中文）

> 写一次插件 → 自动变成 Claude Code Skill、MCP 工具（2026-07-28）和（可选）Claude Code 子代理。零适配代码。

本指南每一条陈述都基于 `fusion-plugins-ecosystem` v0.3.3 的真实 API。英文对照见 [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md)。可运行的场景样例见 [`../examples/`](../examples/)。

---

## 目录

1. [心智模型](#1-心智模型)
2. [30 秒插件](#2-30-秒插件)
3. [PluginManifest —— 每个字段](#3-pluginmanifest--每个字段)
4. [PluginParam —— 类型化输入](#4-pluginparam--类型化输入)
5. [能力 → 自动暴露什么](#5-能力--自动暴露什么)
6. [入口点契约](#6-入口点契约)
7. [`desk` 参数 —— DeskRuntime API](#7-desk-参数--deskruntime-api)
8. [生命周期：加载/启用/执行](#8-生命周期加载启用执行)
9. [异步、超时与长任务](#9-异步超时与长任务)
10. [沙箱模式：INLINE 与 PROCESS](#10-沙箱模式inline-与-process)
11. [类别](#11-类别)
12. [注册与运行](#12-注册与运行)
13. [反向路径：子代理派发](#13-反向路径子代理派发)
14. [MCP 注解与输出 Schema](#14-mcp-注解与输出-schema)
15. [测试清单](#15-测试清单)
16. [样例索引](#16-样例索引)

---

## 1. 心智模型

一个插件 = **一个可调用对象** + **一个清单（manifest）**。

```
your_plugin(desk, params) -> dict     ← 可调用对象（同步或异步）
MY_MANIFEST = PluginManifest(...)      ← 声明
```

- **清单**是声明式的、不可变的（`@dataclass(frozen=True)`），从不持有运行中的实例。它驱动 Claude Skill 生成、MCP 工具定义和生命周期行为。
- **可调用对象**签名是 `(desk: DeskRuntime, params: dict) -> result`。`desk` 是你访问 fusion-cowork 运行时的唯一句柄（日志、显存、MLX、节点注册表）。`params` 是校验后的输入字典。
- 你只需注册清单。生命周期（加载/启用/执行）、MCP 线路和 Claude 适配全部自动完成。

你**绝不**直接 import `fusion_cowork` 内部 —— 一切经由 `desk`。

## 2. 30 秒插件

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

注册并调用：

```python
import fusion_plugins_ecosystem as fpe

registry = fpe.PluginRegistry()
registry.register(MY_ECHO_MANIFEST)
lifecycle = fpe.PluginLifecycle(registry)
await lifecycle.enable("my_echo")           # 加载 + 启用
result = await lifecycle.execute("my_echo", {"text": "hello"})
# → {"echo": "HELLO"}
```

仅凭这个清单还能得到：
- `ClaudeGateway(registry, lifecycle).list_mcp_tools()` → 名为 `mcp__plugin__my_echo` 的 MCP 工具
- `ClaudeGateway(...).export_skills()` → Claude Code Skill 包

## 3. PluginManifest —— 每个字段

`PluginManifest`（`registry.py`）是 frozen dataclass。字段参考：

| 字段 | 类型 | 默认值 | 用途 |
|---|---|---|---|
| `id` | `str` | *(必填)* | 全局唯一插件 id，如 `"my_echo"`。成为 MCP 工具名后缀。 |
| `name` | `str` | *(必填)* | 人类可读的显示名。 |
| `version` | `str` | *(必填)* | 语义化版本。相同 id 注册不同版本会抛 `ValueError`。 |
| `category` | `PluginCategory` | *(必填)* | 8 个类别之一（见 §11）。 |
| `description` | `str` | *(必填)* | 工具列表和 Skill 描述里显示的一句话。 |
| `capabilities` | `tuple[PluginCapability,...]` | `()` | 自动暴露什么（见 §5）。 |
| `params` | `tuple[PluginParam,...]` | `()` | 类型化输入 schema（见 §4）。 |
| `entry_point` | `Callable \| str \| None` | `None` | 可调用对象，或 `"module:attr"` 点分路径（PROCESS 沙箱必填，见 §6/§10）。 |
| `default_mounted` | `bool` | `False` | 为 `True` 时，`config.default_mount_compressor` 开启即自动加载+启用（内置 `caveman_compress` 用此）。 |
| `timeout_seconds` | `int \| None` | `None` | 每次调用超时。`None` → 继承生命周期 `DEFAULT_TIMEOUT=600`。 |
| `vram_mb` | `int` | `0` | 预估显存占用 MB。`0` = 无。 |
| `depends_on` | `tuple[str,...]` | `()` | 需先加载的其他插件 id（通过 `registry.resolve_load_order()` 拓扑排序）。 |
| `sandbox_mode` | `SandboxMode` | `INLINE` | `INLINE`（进程内）或 `PROCESS`（子进程隔离）。见 §10。 |
| `max_restart` | `int \| None` | `None` | 崩溃自动重启上限。`None` → 全局 `MAX_RESTART=3`。 |
| `output_schema` | `dict \| None` | `None` | MCP 2026-07-28 工具结果的 `outputSchema`。 |
| `mcp_annotations` | `MCPAnnotations \| None` | `None` | MCP 工具行为提示（只读/破坏/幂等/开放世界）。见 §14。 |
| `agent_model` | `str \| None` | `None` | 子代理派发的 Claude 模型提示（反向路径，§13）。 |

## 4. PluginParam —— 类型化输入

每个 `PluginParam` 声明一个输入字段。它同时喂给 Claude Skill 参数描述和 MCP 工具 JSON Schema。

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

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | `params` 字典里的键。 |
| `type` | `PluginParamType \| str` | `STRING`/`INT`/`BOOL`/`FLOAT`/`ARRAY`/`OBJECT`。纯字符串（`"string"`）仍兼容，通过 `PARAM_TYPE_TO_JSON_SCHEMA` 映射。 |
| `description` | `str` | 人类描述。 |
| `required` | `bool` | 默认 `False`。 |
| `default` | `Any` | 省略时的默认值。 |
| `enum` | `tuple[str,...] \| None` | 约束为固定集合（如 `("caveman",)`）。 |

入口点里用 `params.get(name, default)` 读取：

```python
text = params.get("text", "")
keep = params.get("keep_comments", False) is True
```

## 5. 能力 → 自动暴露什么

`PluginCapability`（在 `registry.py`）驱动自动暴露。把能力加进元组，每个都有对应效果：

| 能力 | 效果 |
|---|---|
| `MCP_TOOL` | 在 `MCPExporter.list_tools` / `ClaudeGateway.list_mcp_tools` 中以 `mcp__plugin__<id>` 列出，可经 `tools/call` 调用。 |
| `CLAUDE_SKILL` | 由 `SkillAdapter`（及旧版 `ClaudeSkillAdapter`）导出为 Claude Code Skill 包。 |
| `SUBAGENT` | 在 `ClaudeGateway.list_subagent_capable_plugins` 中列出；可作为 `SubagentTask` 的 `plugin_id`（反向派发，§13）。 |
| `VRAM_CONSUMER` | 启用/禁用时经 `DeskRuntime` 申请/释放显存。**仅 INLINE** —— PROCESS 沙箱申请显存会抛 `NotImplementedError`。 |
| `LONG_TASK` | 受超时熔断 + 自动重启约束（≤ `max_restart` / 全局 `MAX_RESTART=3`）。 |
| `FILE_ACCESS` | 文件权限经 `DeskRuntime.check_file_permission` 检查。 |

插件可有多个能力：`caveman_compress` 声明 `(MCP_TOOL, CLAUDE_SKILL)`。

## 6. 入口点契约

每个插件入口点签名 `(desk: DeskRuntime, params: dict) -> result`。

接受四种形式：

1. **可调用对象**（函数或类实例）—— 直接使用。Inline 模式默认。
2. **类** —— 加载时实例化（`lifecycle` 以无参调用 `entry_point()` 构建实例，再 `(instance)(desk, params)`）。
3. **`"module:attr"` 字符串** —— 首次加载时经 `importlib` 解析。**PROCESS 沙箱必填**（子进程必须能 import 它）。
4. **异步可调用对象** —— 由 `PluginLifecycle._invoke_inline`（`lifecycle.py:299`）的 `inspect.iscoroutinefunction(instance)` 自动检测并 await。

**返回值**经 `_format_result`（`jsonrpc.py`）格式化为 MCP：
- `dict` 含 `"content"` 键且为 `list` → 原样用作 MCP content 数组（完全控制）。
- `dict` 不含 `"content"` → JSON 序列化为单个 `{"type":"text","text":<json>}` content 块。
- `str` → `{"type":"text","text":<str>}`。
- `list` → 原样使用（必须是合法 MCP content 数组）。
- 其他 → JSON 序列化文本块。

所以最简单的返回就是普通 `dict`。

**PROCESS 沙箱约束**：入口点**必须是模块级属性** —— 闭包、lambda、局部定义的函数（`__qualname__` 含 `<locals>` 的）无法被子进程 import，spawn 时会失败。用模块级函数/类或 `"module:attr"` 字符串。

## 7. `desk` 参数 —— DeskRuntime API

`desk` 是你访问 fusion-cowork 的唯一桥梁。调它的方法；绝不直接 import `fusion_cowork`。任何字段在降级/离线模式下可能是 `None` —— 注意判空。模块标志 `FUSION_COWORK_AVAILABLE` 表示 `fusion_cowork` 是否可导入。

### DeskRuntime 方法参考（逐字签名）

| 方法 | 签名 | 用途 |
|---|---|---|
| `log` | `log(plugin_id: str, level: str, message: str, **kwargs) -> None` | 统一日志入口 —— 扇出到 logger 并追加到日志环形缓冲。`level` 是字符串名（`"INFO"`/`"WARN"`/`"ERROR"`/`"DEBUG"`）。 |
| `get_logs` | `get_logs(plugin_id: str \| None = None, level: str \| None = None, limit: int = 200) -> list[dict]` | 查询日志缓冲，可按插件/级别过滤；返回最近 `limit` 条。 |
| `acquire_vram` | `acquire_vram(plugin_id: str, mb: int) -> bool` | 申请显存。**resize 语义**（替换 `plugin_id` 之前的分配，非累加）。成功 `True`，超预算 `False`。`mb <= 0` 即释放。 |
| `release_vram` | `release_vram(plugin_id: str) -> None` | 释放 `plugin_id` 持有的显存。 |
| `vram_usage` | `vram_usage() -> dict[str, int]` | 当前显存台账快照 `{plugin_id: mb}`。 |
| `check_file_permission` | `check_file_permission(plugin_id: str, path: str) -> bool` | 检查路径访问。空 `allowed_paths` 白名单 = 全部允许。 |
| `grant_permission` | `grant_permission(plugin_id: str, allowed_paths: list[str]) -> None` | 授予插件路径访问白名单。 |
| `get_api_key` | `get_api_key(provider: str) -> str \| None` | 从配置中心读 API key（`api_keys.{provider}`）。缺失返回 `None`。 |
| `set_api_key` | `set_api_key(provider: str, key: str) -> None` | 写入 API key。 |
| `mlx_chat` | `async mlx_chat(model: str, messages: list[dict], **kwargs) -> Any` | 调 fusion-mlx 本地推理。`mlx_client is None` 时抛 `RuntimeError`。 |
| `mlx_embed` | `async mlx_embed(text: str, model: str = "BGE-M3") -> Any` | 经 fusion-mlx 生成文本向量。`mlx_client is None` 时抛错。 |
| `mlx_health` | `async mlx_health() -> bool` | fusion-mlx 健康检查。`mlx_client is None` 返回 `False`。 |
| `list_nodes` | `list_nodes() -> list[dict]` | 列出已注册的 fusion-cowork 节点类型。无注册表返回 `[]`。 |
| `resolve_node` | `resolve_node(name: str) -> Any \| None` | 解析节点名（别名感知）为类。缺失返回 `None`。 |
| `list_scheduled_tasks` | `list_scheduled_tasks() -> list[Any]` | 列出 fusion-cowork 调度任务。无调度器返回 `[]`。 |
| `gateway_info` | `gateway_info() -> dict` | MCP 网关信息 `{"transport":"stdio","port":<mcp_gateway_port>,"protocol_version":"2026-07-28"}`。 |

### 日志

所有插件日志走 `desk.log(plugin_id, level, message, **fields)` —— **绝不**用 `print()` 或绕过面板的模块级 handler。额外字段成为结构化日志上下文：

```python
desk.log("my_echo", "INFO", "echoed", chars=len(text), ratio=0.7)
```

### 显存

`VRAM_CONSUMER` 插件在启用/禁用时通过 `desk` 自动申请/释放显存（lifecycle 根据 `manifest.vram_mb` 接 `acquire_vram`/`release_vram`）。用 `vram_mb` 声明占用。注意 `acquire_vram` 是 **resize 非累加**：再次调用是替换不是叠加。

### 凭证

`get_api_key`/`set_api_key` 支撑火山方舟 Claude plan 及各 provider key。存于 `api_keys.{provider}`。

## 8. 生命周期：加载/启用/执行

`PluginLifecycle`（`lifecycle.py`）管理实例。类常量：

| 常量 | 值 | 含义 |
|---|---|---|
| `DEFAULT_TIMEOUT` | `600` | 每次调用超时回退值（秒）。 |
| `MAX_RESTART` | `3` | 崩溃自动重启回退上限。 |
| `HEARTBEAT_STALE` | `120` | 无心跳判定卡住的秒数。 |

公开方法（逐字签名）：

| 方法 | 签名 | 用途 |
|---|---|---|
| `load` | `load(plugin_id: str) -> PluginInstance` | 加载：解析入口点（import `"mod:attr"` 或用可调用对象），是 `type` 则实例化，存实例状态 `LOADED`。递归加载 `depends_on`（带环检测）。幂等。 |
| `enable` | `async enable(plugin_id: str) -> PluginInstance` | 启用：必要时自动加载，`vram_mb > 0` 时申请显存，状态 `LOADED→ENABLED`（显存失败则 `→CRASHED`）。 |
| `disable` | `async disable(plugin_id: str) -> None` | 禁用：PROCESS 模式杀沙箱进程，`vram_mb > 0` 时释放显存，转 `DISABLED`。未加载则 no-op。 |
| `unload` | `unload(plugin_id: str) -> None` | 丢弃实例（`vram_mb > 0` 时释放显存）；注册保留。 |
| `execute` | `async execute(plugin_id: str, params: dict, timeout_override: int \| None = None) -> Any` | 带超时熔断执行。**门控：** 除非实例存在**且** `state == ENABLED`，否则抛 `RuntimeError`。超时 → `TIMEOUT` + 自动重启；其他异常 → `CRASHED` + 自动重启。 |
| `list_states` | `list_states() -> list[dict]` | 所有已加载实例的快照字典。 |
| `get_state` | `get_state(plugin_id: str) -> dict \| None` | 单个插件快照，未加载返回 `None`。 |
| `list_by_state` | `list_by_state(state: PluginState) -> list[dict]` | 按 `state` 过滤的实例列表。 |
| `start_watcher` / `stop_watcher` | `async start_watcher() -> None` / `async stop_watcher() -> None` | 启停心跳看门狗后台任务。 |

> **注意：** **没有公开的 `reload()`**。崩溃恢复是私有的 `_maybe_restart`（unload → load → enable，受 `max_restart ?? MAX_RESTART` 限制）。要强制全新实例，先 `unload` 再 `enable`。

`PluginState` 枚举成员：`REGISTERED`、`LOADED`、`ENABLED`、`DISABLED`、`CRASHED`、`TIMEOUT`。

## 9. 异步、超时与长任务

- **异步**：声明 `async def my_plugin(desk, params)`；lifecycle 经 `inspect.iscoroutinefunction` 检测并 await。见 `examples/02_async/`。
- **超时**：`manifest.timeout_seconds`（或全局 `DEFAULT_TIMEOUT=600`，或 `execute(..., timeout_override=)`）。超时后插件转 `TIMEOUT`，错误计数 +1，`_maybe_restart` 可能重启（上限 `max_restart`）。
- **长任务**：声明 `LONG_TASK` 能力走熔断+重启路径。见 `examples/07_long_task/`。

## 10. 沙箱模式：INLINE 与 PROCESS

`manifest.sandbox_mode`（`SandboxMode` 枚举，`schema.py`）：

| 模式 | 行为 | 何时用 |
|---|---|---|
| `INLINE`（默认） | 经 `_invoke_inline` 进程内运行。支持异步、闭包、`VRAM_CONSUMER`。 | 大多数插件。 |
| `PROCESS` | 经 `PluginSandbox` 在隔离子进程运行（IPC + 心跳 + `ResourceLimits`）。入口点**必须是模块级属性**或 `"module:attr"`。**不能申请显存**（抛 `NotImplementedError`）。 | 需崩溃隔离的不可信/重型插件。 |

`_invoke`（`lifecycle.py:277`）选择优先级：
1. `manifest.sandbox_mode == PROCESS` → 沙箱。
2. 否则若 `config.sandbox_default_mode == "process"` → 沙箱。
3. 否则 → inline。

见 `examples/08_process_sandbox/`。

## 11. 类别

`PluginCategory`（8 个值）：

| 类别 | 值 | 示例 |
|---|---|---|
| `CODING_PLAN` | `coding_plan` | coding-plan 加速器 |
| `CONTEXT_COMPRESS` | `context_compress` | `caveman_compress`（内置） |
| `MLX_INFERENCE` | `mlx_inference` | 本地 MLX 模型推理 |
| `TERMINAL_PROXY` | `terminal_proxy` | 终端命令代理 |
| `FILE_INDEX` | `file_index` | 本地文件搜索/索引 |
| `QUANTIZATION` | `quantization` | 混合量化工具 |
| `VISUAL_BACKEND` | `visual_backend` | Claude 图像/视觉生成后端 |
| `CUSTOM` | `custom` | 其他 |

`registry.list(category=...)` 按此过滤。

## 12. 注册与运行

```python
import fusion_plugins_ecosystem as fpe
from my_plugin_pkg import MY_ECHO_MANIFEST   # 你的清单模块

registry = fpe.PluginRegistry()
registry.register(MY_ECHO_MANIFEST)          # 或 registry.register_builtin()
lifecycle = fpe.PluginLifecycle(registry)

await lifecycle.enable("my_echo")            # 加载实例 + state=ENABLED
result = await lifecycle.execute("my_echo", {"text": "hi"})   # 运行
await lifecycle.disable("my_echo")           # state=DISABLED
lifecycle.unload("my_echo")                  # 丢弃实例（注册保留）
```

执行门控：`lifecycle.execute` 除非 `inst.state == ENABLED`（`lifecycle.py:248`）否则抛 `RuntimeError("插件 ... 未启用")`。所以**先 enable 再 execute**。（`default_mounted=True` + `config.default_mount_compressor` 在 `MCPServer.start()` 自动启用。）

### 独立 MCP server

```bash
fusion-plugin-server --transport stdio              # Claude Desktop 子进程
fusion-plugin-server --transport sse  --port 8765   # 远程 SSE
fusion-plugin-server --transport http --port 8765   # Streamable HTTP（MCP 2026-07-28）
```

`start()` 注册内置插件、自动挂载 `default_mounted` 插件、在 stop event 上阻塞保活，并服务 `tools/list` + `tools/call`。

## 13. 反向路径：子代理派发

fusion-cowork 可拉一个 Claude Code 子代理**来**运行你的插件（反向方向）。你的插件需要 `SUBAGENT` 能力，（可选）`agent_model`。

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

`SubagentTask` 字段：`name`、`plugin_id`、`arguments`、`timeout_seconds`（None→config）、`metadata`（dict）。超时/崩溃时，`config.subagent_timeout_destroy` 决定自动销毁还是保留。见 `examples/04_subagent/`。

## 14. MCP 注解与输出 Schema

`MCPAnnotations`（`schema.py`，frozen dataclass）—— MCP 2026-07-28 行为提示：

```python
from fusion_plugins_ecosystem.schema import MCPAnnotations

manifest = PluginManifest(
    ...,
    mcp_annotations=MCPAnnotations(
        readOnlyHint=True,      # 安全，无副作用
        destructiveHint=False,
        idempotentHint=True,    # 重复调用 = 相同结果
        openWorldHint=False,    # 封闭/已知世界
    ),
    output_schema={             # MCP outputSchema（JSON Schema）
        "type": "object",
        "properties": {
            "echo": {"type": "string"},
        },
        "required": ["echo"],
    },
)
```

默认：所有 hint 为 `False`，除 `openWorldHint=True`。

## 15. 测试清单

- [ ] 清单字段齐全（`id`/`name`/`version`/`category`/`description`/`entry_point`）。
- [ ] `params` 类型用 `PluginParamType`；入口点用 `params.get(name, default)` 读。
- [ ] 日志走 `desk.log(...)`，绝不 `print()`。
- [ ] 异步入口点声明 `async def`（lifecycle 自动检测）。
- [ ] `execute()` 前先 `enable()`（或 `default_mounted=True`）。
- [ ] PROCESS 沙箱入口点是模块级属性或 `"module:attr"`（无闭包）。
- [ ] `VRAM_CONSUMER` 插件用 `INLINE` 模式（PROCESS 不能申请显存）。
- [ ] 长任务设超时；声明 `LONG_TASK` 能力。
- [ ] 返回值是普通 `dict`（或含 `content` list 的 dict 以自定义 MCP 块）。
- [ ] 测试用 `pytest` + `asyncio_mode=auto`（无需 `@pytest.mark.asyncio`）。清理临时数据，只保留最终输出 + 日志。

## 16. 样例索引

[`../examples/`](../examples/) 中的可运行场景，每个自包含（清单、测试、README）：

| # | 场景 | 教什么 |
|---|---|---|
| 01 | `echo` | 最小同步插件，`MCP_TOOL` + `CLAUDE_SKILL` |
| 02 | `async_fetch` | 异步入口点，自动检测 |
| 03 | `skill_export` | `CLAUDE_SKILL` → Skill 包检视 |
| 04 | `subagent_reverse` | `SUBAGENT` 能力 + `dispatch_subagent` |
| 05 | `mlx_chat` | `MLX_INFERENCE`，`desk.mlx_chat()` |
| 06 | `vram_consumer` | `VRAM_CONSUMER` + `vram_mb`，仅 INLINE |
| 07 | `long_task` | `LONG_TASK` + 超时熔断 + 重启 |
| 08 | `process_sandbox` | `SandboxMode.PROCESS`，`module:attr` |
| 09 | `file_access` | `FILE_ACCESS` + `desk.check_file_permission` |
