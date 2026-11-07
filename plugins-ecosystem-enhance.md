---
doc_id: plugins-ecosystem-enhance/design-notes
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: guide
title: {zh: "重构增强方案", en: "Enhancement Design Notes"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - plugins-ecosystem-enhance.md
---

# fusion-plugins-ecosystem 重构增强方案

> 版本: 1.0
> 日期: 2026-08-02
> 状态: 设计中

---

## 一、执行摘要

基于对 Claude 插件生态（MCP 协议 2026-07-28、Claude Code Skills/Agents/Plugins 体系）的深度洞察，以及对当前 fusion-plugins-ecosystem 实现的全面审计，本方案提出分层重构策略：

**核心结论：当前实现是一个"声明式外壳"，协议层、传输层、沙箱层均为 mock/stub，与真实 Claude 生态的差距是结构性的，非修补可解。需要从根本上重建 MCP 协议栈、插件沙箱、Skill/Agent 适配层，同时保留已有的注册中心和生命周期管理架构。**

---

## 二、现状诊断

### 2.1 架构评分：D+

| 维度 | 评分 | 说明 |
|------|------|------|
| MCP 协议合规 | F | 仅生成 tool 描述，无 JSON-RPC 处理、无传输层、无会话管理 |
| Claude Skill 适配 | D | 生成 dict 但不符合 SKILL.md 规范，无渐进式加载 |
| Claude Agent 适配 | F | "子代理"实际是同进程函数调用，非 Claude Code Agent |
| 插件沙箱 | F | 全部进程内执行，无隔离 |
| fusion-desk 集成 | F | 依赖的 fusion-desk 源码不存在，全部 mock |
| 注册中心 | B | 声明式 manifest 设计合理，但可变性、线程安全有问题 |
| 生命周期管理 | C+ | 状态机有缺陷但框架在，超时熔断逻辑有 |
| Token 计量 | C | 框架在，但无限增长、无持久化、无异步感知 |

### 2.2 关键缺陷清单

#### A 级 — 架构性缺陷（必须重构）

| # | 缺陷 | 影响 |
|---|------|------|
| A1 | **无真实 MCP 服务器**：MCPExporter.call_tool() 是死代码，无 JSON-RPC handler，无传输层 | Claude Desktop/Code 无法连接 |
| A2 | **MCP 协议版本过时**：硬编码 `2024-11-05`，最新 `2026-07-28` 新增 resultType、server/discover、structuredContent | 协议不兼容 |
| A3 | **无插件沙箱**：所有插件进程内执行，崩溃影响宿主 | 安全性、稳定性均无保障 |
| A4 | **fusion-desk 依赖断裂**：源码目录不存在，DeskRuntime 全部 mock | 项目无法接入真实运行时 |
| A5 | **ClaudeGateway.dispatch_subagent 篡改共享 manifest**：并发 dispatch 时竞态条件 | 数据损坏 |
| A6 | **DeskContext 完全多余**：零行为纯转发，增加理解成本 | 代码冗余 |
| A7 | **Claude Skill 适配不符合规范**：输出 dict 而非 SKILL.md，无渐进式加载 | 无法被 Claude Code 识别 |

#### B 级 — 正确性缺陷（必须修复）

| # | 缺陷 | 影响 |
|---|------|------|
| B1 | PluginManifest 是可变 dataclass | 注册后可被意外修改 |
| B2 | caveman_compress 剥离 URL 中的 `//` | `https://` 被破坏 |
| B3 | `bool("False")` 为 True — 参数解析错误 | keep_comments 参数反直觉 |
| B4 | TokenMeter._records 无限增长 | 长时间运行内存泄漏 |
| B5 | vram_allocations 不是线程安全的 | 并发 acquire/release 可能数据竞争 |
| B6 | enable()/disable() 标记 async 但无异步操作 | 误导性 API |
| B7 | 状态机无转换验证 | DISABLED→ENABLED 跳过 LOADED |
| B8 | 心跳仅在 execute() 开始时设置 | 长时间同步插件被误杀 |
| B9 | PluginParam.type 自由字符串无验证 | 无效类型静默降级 |
| B10 | EcosystemConfig.from_dict 类型强制转换错误 | `int(True)` = 1 |
| B11 | caveman_compress 声明 LONG_TASK 但实际是快速同步操作 | 不必要的超时/心跳开销 |
| B12 | acquire_vram 同一 plugin_id 重复调用累加 | 无调整大小机制 |
| B13 | check_file_permission 前缀匹配无标准化 | `/data` 匹配 `/datastash` |

#### C 级 — 缺失功能（PRD 承诺 vs 实际）

| # | 缺失 | PRD 承诺 |
|---|------|----------|
| C1 | MCP 传输层（stdio/SSE/HTTP） | "融合 MCP 服务器暴露" |
| C2 | 插件进程隔离 | "进程托管" |
| C3 | 真正的 Claude Code 子代理调度 | "Claude Code 子代理调度" |
| C4 | Claude Skill 渐进式加载 | "全链路适配" |
| C5 | 插件间依赖声明 | 无 |
| C6 | 插件版本冲突处理 | 无 |
| C7 | 内置插件自动发现机制 | register_builtin() 硬编码 |
| C8 | MCP annotations（readOnlyHint 等） | MCP 规范要求 |
| C9 | API 密钥加密存储 | 明文存储 |
| C10 | Token 计量持久化和时间范围查询 | "统一计量" |
| C11 | 配置变更回调通知 | 无 |
| C12 | 请求速率限制 | 无 |
| C13 | MCP 会话管理 | MCP 规范要求 |
| C14 | 输出 Schema（outputSchema） | MCP 2026-07-28 新增 |

### 2.3 与 Claude 官方生态的对标差距

```
Claude 官方生态                     fusion-plugins-ecosystem 当前
─────────────────                   ──────────────────────────────
MCP Server (JSON-RPC 2.0)     →    无服务器，仅生成描述
  stdio 传输                   →    硬编码 "stdio" 字符串
  SSE/HTTP 传输                →    不支持
  能力协商 (server/discover)   →    不支持
  会话管理                     →    不支持
  tools/list + tools/call      →    list_tools 生成描述，call_tool 死代码
  resources/*                  →    不支持
  prompts/*                    →    不支持

Claude Code Plugin             →    不支持
  .claude-plugin/plugin.json   →    无
  commands/ (斜杠命令)          →    无
  agents/ (.md agent 定义)     →    无
  skills/ (SKILL.md)           →    生成 dict 而非文件
  hooks/ (事件处理器)           →    无

Claude Skill (SKILL.md)        →    生成 Python dict
  渐进式加载 (3层)             →    无
  references/ 按需加载          →    无
  scripts/ 可执行工具           →    无

Claude Agent (.md 定义)        →    SubagentTask dataclass
  model 选择                   →    无
  tools 限制                   →    无
  并行调度                     →    不支持

MCP Registry                   →    无
  server.json 发布格式         →    无
  命名空间验证                 →    无
```

---

## 三、目标架构

### 3.1 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                     Claude Desktop / Code / Web             │
│                       (MCP Client / Host)                   │
└──────────┬──────────────────────────────┬───────────────────┘
           │ MCP (stdio/SSE/HTTP)         │ Anthropic API
           ▼                              ▼
┌──────────────────────┐    ┌──────────────────────────────────┐
│   MCP Server Layer   │    │    Claude API Adapter            │
│   (JSON-RPC 2.0)     │    │    (Skill/Agent/Tool 转换)       │
│                      │    │                                  │
│  ┌────────────────┐  │    │  ┌────────────┐ ┌─────────────┐ │
│  │ Transport      │  │    │  │SkillAdapter│ │AgentAdapter │ │
│  │ (stdio/SSE/   │  │    │  │→ SKILL.md  │ │→ agent .md  │ │
│  │  HTTP/WS)      │  │    │  └────────────┘ └─────────────┘ │
│  └───────┬────────┘  │    │  ┌────────────┐ ┌─────────────┐ │
│          │           │    │  │ToolAdapter │ │HookAdapter  │ │
│  ┌───────▼────────┐  │    │  │→ API tool  │ │→ HookEvent  │ │
│  │ JSON-RPC       │  │    │  └────────────┘ └─────────────┘ │
│  │ Handler        │  │    └──────────────────────────────────┘
│  │ (tools/resources│  │                    │
│  │  /prompts)     │  │                    │
│  └───────┬────────┘  │                    │
└──────────┼───────────┘                    │
           │                                │
           ▼                                ▼
┌─────────────────────────────────────────────────────────────┐
│                    Plugin Runtime Core                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ Registry     │  │ Lifecycle    │  │ Sandbox           │ │
│  │ (不可变      │  │ (状态机      │  │ (进程隔离/        │ │
│  │  Manifest)   │  │  严格转换)   │  │  资源限额)        │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬────────────┘ │
│         │                 │                  │              │
│  ┌──────▼─────────────────▼──────────────────▼────────────┐ │
│  │              Desk Bridge (Protocol)                     │ │
│  │  vRAM | Log | Permission | MLX | Config | Scheduler    │ │
│  └──────────────────────┬─────────────────────────────────┘ │
└─────────────────────────┼───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│              fusion-desk Runtime (上游)                       │
│  MCP Gateway | Hardware Scheduler | Config Center | Logger   │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 层级职责重新定义

| 层 | 模块 | 职责 | 变化 |
|----|------|------|------|
| L1 | Transport | MCP 传输协议实现（stdio/SSE/HTTP） | **新增** |
| L2 | JSON-RPC | MCP 协议处理（tools/resources/prompts） | **新增** |
| L3 | Adapter | Claude Skill/Agent/Tool/Hook 格式转换 | **重构** |
| L4 | Lifecycle | 插件状态机 + 超时熔断 + 自动重启 | **修复** |
| L5 | Sandbox | 进程隔离 + 资源限额 | **新增** |
| L6 | Registry | 不可变 Manifest 存储 + 依赖解析 | **重构** |
| L7 | Desk Bridge | fusion-desk 协议桥接（合并 DeskRuntime + DeskContext） | **合并** |
| L8 | Metering | Token 计量（持久化 + 时间范围） | **增强** |
| L9 | Config | 配置面板（验证 + 变更通知） | **增强** |

### 3.3 新增模块说明

#### L1: Transport (`transport.py`)

```python
class Transport(ABC):
    """MCP 传输抽象基类"""
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: dict) -> None: ...
    def on_message(self, handler: Callable) -> None: ...

class StdioTransport(Transport):
    """stdin/stdout JSON-RPC，用于 Claude Desktop 本地子进程"""

class SSETransport(Transport):
    """HTTP SSE 传输，用于远程客户端"""

class HTTPTransport(Transport):
    """Streamable HTTP 传输 (MCP 2026-07-28)"""
```

#### L2: JSON-RPC Handler (`jsonrpc.py`)

```python
class MCPHandler:
    """MCP JSON-RPC 2.0 请求处理器"""
    async def handle(self, request: dict) -> dict: ...

    # MCP 方法实现
    async def _initialize(self, params: dict) -> dict: ...
    async def _tools_list(self, params: dict) -> dict: ...
    async def _tools_call(self, params: dict) -> dict: ...
    async def _resources_list(self, params: dict) -> dict: ...
    async def _resources_read(self, params: dict) -> dict: ...
    async def _prompts_list(self, params: dict) -> dict: ...
    async def _prompts_get(self, params: dict) -> dict: ...
    async def _server_discover(self, params: dict) -> dict: ...
```

#### L5: Sandbox (`sandbox.py`)

```python
class PluginSandbox:
    """插件进程隔离沙箱"""
    async def spawn(self, plugin_id: str, entry_point: str) -> SandboxHandle: ...
    async def kill(self, handle: SandboxHandle) -> None: ...
    async def call(self, handle: SandboxHandle, method: str, args: dict) -> Any: ...

class SandboxHandle:
    """沙箱内插件句柄"""
    pid: int
    vram_budget_mb: int
    cpu_limit: float
    memory_limit_mb: int
```

#### L3: Adapter 重构

```python
class SkillAdapter:
    """生成 SKILL.md 格式的 Claude Skill"""
    def export_skill(self, manifest: PluginManifest) -> SkillBundle: ...

class SkillBundle:
    """符合 Claude Code 规范的 Skill 包"""
    skill_md: str               # SKILL.md 内容
    references: dict[str, str]  # references/*.md
    scripts: dict[str, str]     # scripts/* 可执行

class AgentAdapter:
    """生成 .md 格式的 Claude Agent 定义"""
    def export_agent(self, manifest: PluginManifest) -> str: ...

class HookAdapter:
    """生成 Hook 事件定义"""
    def export_hooks(self, manifest: PluginManifest) -> list[HookDef]: ...
```

---

## 四、PRD — 产品需求定义

### 4.1 核心需求

#### P0: MCP 协议服务器

**目标**：fusion-plugins-ecosystem 作为真正的 MCP Server，可被 Claude Desktop、Claude Code、Web Claude 直接连接。

**功能点**：

| ID | 功能 | 验收标准 |
|----|------|----------|
| MCP-01 | JSON-RPC 2.0 处理器 | 解析/响应所有 MCP 规范方法 |
| MCP-02 | stdio 传输 | Claude Desktop 可作为子进程启动并连接 |
| MCP-03 | SSE 传输 | 远程客户端可通过 HTTP SSE 连接 |
| MCP-04 | Streamable HTTP 传输 | 支持 MCP 2026-07-28 最新传输协议 |
| MCP-05 | tools/list | 返回所有 MCP_TOOL 插件的完整描述（含 annotations、outputSchema） |
| MCP-06 | tools/call | 真正调用插件并返回 MCP 规范响应 |
| MCP-07 | resources/list + resources/read | 暴露插件资源（文件、数据） |
| MCP-08 | prompts/list + prompts/get | 暴露插件提示模板 |
| MCP-09 | server/discover | 能力协商，返回支持的能力集 |
| MCP-10 | initialize + initialized | 会话初始化握手 |
| MCP-11 | 协议版本 | 支持 `2026-07-28`，向后兼容 `2024-11-05` |
| MCP-12 | 请求 ID 跟踪 | 每个请求/响应携带唯一 ID |

#### P1: 插件沙箱

**目标**：插件在独立进程中运行，崩溃不影响宿主。

| ID | 功能 | 验收标准 |
|----|------|----------|
| SBX-01 | 进程隔离 | 每个插件可选独立子进程运行 |
| SBX-02 | vRAM 预算 | 通过 cgroups/Apple sandbox 限制 GPU 内存 |
| SBX-03 | CPU 限制 | 限制插件 CPU 使用率 |
| SBX-04 | 内存限制 | 限制插件进程内存 |
| SBX-05 | 超时强杀 | 超时后 SIGKILL 子进程 |
| SBX-06 | 优雅退出 | SIGTERM → 等待 grace_period → SIGKILL |
| SBX-07 | 进程内模式 | 轻量插件可选择进程内运行（向后兼容） |

#### P2: Claude 生态适配

**目标**：插件自动转换为 Claude Code Plugin 格式。

| ID | 功能 | 验收标准 |
|----|------|----------|
| CLD-01 | Skill 生成 | PluginManifest → SKILL.md（含 frontmatter + body） |
| CLD-02 | Skill 渐进式加载 | 元数据层 + body 层 + references 层 |
| CLD-03 | Agent 生成 | PluginManifest → agent .md（含 YAML frontmatter） |
| CLD-04 | Agent model 选择 | 支持 inherit/sonnet/opus/haiku |
| CLD-05 | Hook 定义 | PluginManifest → Hook 事件映射 |
| CLD-06 | .claude-plugin 生成 | 生成完整 plugin.json + 目录结构 |
| CLD-07 | MCP 服务器命名空间 | 工具名 `mcp__plugin_<name>__<tool>` |

#### P3: 注册中心增强

**目标**：支持插件依赖、版本、自动发现。

| ID | 功能 | 验收标准 |
|----|------|----------|
| REG-01 | 不可变 Manifest | dataclass(frozen=True) |
| REG-02 | 依赖声明 | `depends_on: list[str]` 字段 |
| REG-03 | 依赖解析 | 加载时按拓扑序初始化 |
| REG-04 | 版本冲突检测 | 同 id 不同版本拒绝注册 |
| REG-05 | 内置插件自动发现 | 扫描 builtin/ 目录，自动注册 |
| REG-06 | Param 类型枚举 | PluginParamType enum 替代自由字符串 |
| REG-07 | entry_point 注册时验证 | 字符串路径导入失败立即报错 |

#### P4: 生命周期修复

**目标**：严格状态机 + 线程安全 + 心跳修复。

| ID | 功能 | 验收标准 |
|----|------|----------|
| LIF-01 | 严格状态转换 | 每个转换有合法前驱状态校验 |
| LIF-02 | per-plugin 配置 | timeout_seconds、max_restart 可 per-plugin 覆盖 |
| LIF-03 | 心跳在线更新 | execute() 内周期性更新心跳（非仅开始时） |
| LIF-04 | 线程安全 | _instances、vram_allocations 使用 asyncio.Lock |
| LIF-05 | 状态查询 API | get_state(plugin_id)、list_by_state(state) |

#### P5: Token 计量增强

**目标**：持久化 + 时间范围 + 限制增长。

| ID | 功能 | 验收标准 |
|----|------|----------|
| TOK-01 | 记录修剪 | 超过 max_records 自动淘汰最旧记录 |
| TOK-02 | 时间范围查询 | summary(since=timestamp) |
| TOK-03 | 持久化 | 可选写入 SQLite / JSON 文件 |
| TOK-04 | 异步上下文管理器 | measure() 支持 async with |
| TOK-05 | 无 token 阈值可配置 | 不硬编码 60s |

#### P6: 配置增强

**目标**：验证 + 变更通知。

| ID | 功能 | 验收标准 |
|----|------|----------|
| CFG-01 | 值域验证 | timeout_seconds > 0, max_auto_restart ∈ [0, 100] |
| CFG-02 | from_dict 安全解析 | 缺失字段用默认值，类型不匹配抛出明确错误 |
| CFG-03 | 变更回调 | on_change(key, old_value, new_value) 回调机制 |
| CFG-04 | 与 Desk 配置中心同步 | 双向读写 config_center |

### 4.2 GUI 需求（提交给 fusion-studio）

以下需求需提 issue 给 fusion-studio：

| ID | 功能 | 说明 |
|----|------|------|
| GUI-01 | 插件市场面板 | 浏览/搜索/安装/卸载插件 |
| GUI-02 | 插件配置面板 | EcosystemConfig 可视化编辑 |
| GUI-03 | 插件状态监控 | 实时显示插件状态（LOADED/ENABLED/CRASHED 等） |
| GUI-04 | Token 用量仪表盘 | 按 plugin/kind 维度的 token 消耗图表 |
| GUI-05 | vRAM 分配可视化 | 显示各插件 vRAM 占用 |
| GUI-06 | 插件日志查看器 | 实时流式显示插件日志 |
| GUI-07 | MCP 连接管理 | 显示已连接的 MCP 客户端，管理传输类型 |

**IPC 接口设计（JSON-RPC over UDS）**：

```json
{
    "plugin.list": "列出所有插件",
    "plugin.get": {"id": "string"} -> "获取插件详情",
    "plugin.install": {"source": "string"} -> "安装插件",
    "plugin.uninstall": {"id": "string"} -> "卸载插件",
    "plugin.enable": {"id": "string"} -> "启用插件",
    "plugin.disable": {"id": "string"} -> "禁用插件",
    "plugin.config": {"id": "string", "config": {}} -> "配置插件",
    "plugin.state": {"id": "string"} -> "查询状态",
    "plugin.logs": {"id": "string", "follow": false} -> "获取日志",
    "ecosystem.config": {} -> "获取/设置生态配置",
    "ecosystem.mcp_status": {} -> "MCP 连接状态",
    "ecosystem.token_summary": {"since": "timestamp?"} -> "Token 用量",
    "ecosystem.vram_usage": {} -> "vRAM 使用"
}
```

### 4.3 上游需求（提交 Issue + PR）

#### fusion-desk Issue

| Issue | 标题 | 说明 |
|-------|------|------|
| FD-01 | 创建 fusion-desk 源码项目 | 当前源码目录不存在，整个依赖链断裂 |
| FD-02 | NodeRegistry API 定义 | 需要 `list()` / `get(name)` 接口定义 |
| FD-03 | TaskScheduler API 定义 | 需要 `list_tasks()` 接口定义 |
| FD-04 | FusionMLXClient API 定义 | 需要 `chat()` / `embed()` / `health()` 接口定义 |
| FD-05 | ConfigCenter API 定义 | 需要 `get(key)` / `set(key, value)` 接口定义 |
| FD-06 | MCP Gateway 实现 | 真实的 MCP 服务器进程管理 |
| FD-07 | 插件沙箱基础设施 | 进程隔离、资源限额的底层支持 |

#### fusion-gateway Issue

| Issue | 标题 | 说明 |
|-------|------|------|
| FG-01 | ClaudeGateway 边界明确 | ARCHITECTURE_COMPLIANCE.md 已识别：如为通用 API 网关能力考虑迁至 fusion-gateway |
| FG-02 | MCP 路由支持 | fusion-gateway 是否应支持 MCP 协议路由 |

---

## 五、重构实施计划

### Phase 0: 基础修复（1-2 天）

**目标**：修复所有 B 级正确性缺陷，不改变外部 API。

| 任务 | 文件 | 变更 |
|------|------|------|
| 冻结 PluginManifest | registry.py | `@dataclass(frozen=True)` |
| 冻结 PluginParam | registry.py | `@dataclass(frozen=True)` |
| 冻结 PluginInstance | lifecycle.py | `@dataclass(frozen=True)`，改用替换而非修改 |
| 修复 dispatch_subagent 竞态 | claude_gateway.py | 复制 manifest 而非修改共享实例 |
| 修复 caveman URL 剥离 | builtin/caveman_compress.py | 改正则：仅匹配行首 `//`，不匹配 `https://` |
| 修复 bool 参数解析 | builtin/caveman_compress.py | `params.get("keep_comments", False) is True` |
| 移除 LONG_TASK 声明 | builtin/caveman_compress.py | caveman 是快速同步操作 |
| TokenMeter 修剪 | token_meter.py | 添加 max_records=10000，FIFO 淘汰 |
| EcosystemConfig 验证 | config.py | from_dict 安全解析 + 值域检查 |
| 线程安全 | desk_runtime.py, registry.py | asyncio.Lock 保护共享状态 |
| 移除 DeskContext | desk_context.py | 合并到 DeskRuntime，更新所有引用 |
| 修复 enable/disable async | lifecycle.py | 去掉不必要的 async 声明，或添加真正的异步操作 |
| 状态转换验证 | lifecycle.py | 添加 _valid_transitions 映射 |
| 心跳在线更新 | lifecycle.py | execute() 内周期性更新 last_heartbeat |
| 统一类型映射 | 新建 schema.py | 提取共享 _PARAM_TYPE_MAP |
| caveman 策略参数移除 | builtin/caveman_compress.py | 移除无用的 strategy 参数或实现多策略 |
| register_builtin 自动发现 | registry.py | 扫描 builtin/ 包，自动导入注册 |
| 修复 check_file_permission | desk_runtime.py | 路径标准化 + 精确匹配 |

### Phase 1: MCP 协议栈（3-5 天）

**目标**：实现真正的 MCP Server，可被 Claude Desktop 连接。

#### 1.1 传输层 (`transport.py`)

```
Transport (ABC)
├── StdioTransport    — stdin/stdout, 用于 Claude Desktop 子进程
├── SSETransport      — HTTP + SSE, 用于远程客户端
└── HTTPTransport     — Streamable HTTP (MCP 2026-07-28)
```

**关键设计**：
- 每个传输实例绑定一个 MCPHandler
- 支持同时运行多个传输（例如 stdio + SSE）
- 消息格式：单行 JSON-RPC（stdio）/ HTTP POST + SSE（SSE）/ HTTP POST + JSON/SSE（HTTP）

#### 1.2 JSON-RPC 处理器 (`jsonrpc.py`)

```python
class MCPHandler:
    """MCP JSON-RPC 2.0 请求分发器"""

    PROTOCOL_VERSION = "2026-07-28"

    def __init__(self, registry, lifecycle, desk, config, token_meter): ...

    # 协议方法
    _METHODS = {
        "initialize": _initialize,
        "initialized": _initialized,
        "server/discover": _server_discover,
        "tools/list": _tools_list,
        "tools/call": _tools_call,
        "resources/list": _resources_list,
        "resources/read": _resources_read,
        "prompts/list": _prompts_list,
        "prompts/get": _prompts_get,
        "ping": _ping,
    }

    async def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        handler = self._METHODS.get(method)
        if handler is None:
            return error_response(request["id"], -32601, "Method not found")
        return await handler(self, request.get("params", {}))
```

#### 1.3 MCP 工具描述增强

当前 `_manifest_to_mcp_tool` 输出：
```json
{"name": "x", "description": "...", "inputSchema": {...}}
```

需增强为符合 MCP 2026-07-28 规范：
```json
{
    "name": "x",
    "title": "Plugin Name",
    "description": "...",
    "inputSchema": {...},
    "outputSchema": {...},
    "annotations": {
        "readOnlyHint": false,
        "destructiveHint": false,
        "idempotentHint": false,
        "openWorldHint": true
    }
}
```

#### 1.4 MCP Server 启动入口

```python
# 新文件: server.py
class MCPServer:
    """MCP Server 入口，组合传输 + 处理器 + 运行时"""

    def __init__(self, config: EcosystemConfig | None = None): ...

    async def start(self, transport: str = "stdio", **kwargs) -> None:
        """启动 MCP Server"""
        transport_inst = self._create_transport(transport, **kwargs)
        await transport_inst.start()

    async def stop(self) -> None: ...

    # CLI 入口
    def main():
        """pip 安装后的 fusion-plugin-server 命令入口"""
        import asyncio
        server = MCPServer()
        asyncio.run(server.start(transport="stdio"))
```

`pyproject.toml` 新增：
```toml
[project.scripts]
fusion-plugin-server = "fusion_plugins_ecosystem.server:main"

[project.optional-dependencies]
mcp = ["mcp>=1.0.0"]
```

### Phase 2: 插件沙箱（3-4 天）

**目标**：插件可运行在独立进程中。

#### 2.1 沙箱架构

```
PluginSandbox (宿主侧)
    │
    ├── spawn(plugin_id, entry_point, config)
    │      ↓ fork / subprocess
    │   SandboxWorker (子进程侧)
    │      │
    │      ├── 加载 entry_point
    │      ├── 监听 stdin 命令
    │      └── 报告心跳
    │
    ├── call(plugin_id, method, args) → result
    ├── kill(plugin_id)
    └── health(plugin_id) → SandboxHealth
```

#### 2.2 IPC 协议（子进程内）

子进程通过 stdin/stdout JSON 消息与宿主通信：

```json
// 宿主 → 子进程
{"type": "call", "id": "uuid", "method": "execute", "args": {...}}

// 子进程 → 宿主
{"type": "result", "id": "uuid", "result": {...}}
{"type": "heartbeat", "timestamp": 1234567890}
{"type": "log", "level": "INFO", "message": "...", "kw": {}}
{"type": "error", "id": "uuid", "error": "..."}
```

#### 2.3 资源限制（Apple Silicon）

```python
class ResourceLimits:
    vram_budget_mb: int = 0         # GPU 内存预算
    memory_limit_mb: int = 512      # 进程内存上限
    cpu_limit: float = 1.0          # CPU 核心数上限
    timeout_seconds: int = 600      # 执行超时
    grace_period_seconds: int = 10  # SIGTERM → SIGKILL 间隔
```

macOS 实现：通过 `sandbox-exec`（Apple Sandbox Profile）+ `resource_limits`（setrlimit）。

#### 2.4 进程内/进程外双模式

```python
class PluginManifest:
    # 新增字段
    sandbox_mode: SandboxMode = SandboxMode.INLINE  # INLINE | PROCESS
```

- `INLINE`：进程内执行（向后兼容，轻量插件）
- `PROCESS`：独立子进程（需沙箱隔离的插件）

### Phase 3: Claude 生态适配（2-3 天）

**目标**：插件自动转换为 Claude Code Plugin 格式。

#### 3.1 Skill 生成器

```python
class SkillAdapter:
    def export_skill(self, manifest: PluginManifest) -> SkillBundle:
        """生成符合 Claude Code 规范的 Skill 包"""
        skill_md = self._generate_skill_md(manifest)
        references = self._generate_references(manifest)
        scripts = self._generate_scripts(manifest)
        return SkillBundle(
            skill_md=skill_md,
            references=references,
            scripts=scripts,
        )

    def _generate_skill_md(self, manifest: PluginManifest) -> str:
        """生成 SKILL.md 文件内容"""
        frontmatter = yaml.dump({
            "name": manifest.name,
            "description": self._build_trigger_description(manifest),
            "version": manifest.version,
        })
        body = self._build_skill_body(manifest)
        return f"---\n{frontmatter}---\n\n{body}"
```

#### 3.2 Agent 生成器

```python
class AgentAdapter:
    def export_agent(self, manifest: PluginManifest) -> str:
        """生成 agent .md 文件"""
        frontmatter = yaml.dump({
            "name": manifest.id,
            "description": self._build_agent_description(manifest),
            "model": manifest.agent_model or "inherit",
            "color": self._category_to_color(manifest.category),
            "tools": self._capability_to_tools(manifest.capabilities),
        })
        body = self._build_agent_body(manifest)
        return f"---\n{frontmatter}---\n\n{body}"
```

#### 3.3 Plugin Bundle 生成器

```python
class PluginBundleGenerator:
    def generate(self, manifest: PluginManifest) -> PluginBundle:
        """生成完整 Claude Code Plugin 目录结构"""
        return PluginBundle(
            plugin_json=self._generate_plugin_json(manifest),
            skills=skill_adapter.export_skill(manifest),
            agents=agent_adapter.export_agent(manifest) if PluginCapability.SUBAGENT in manifest.capabilities else None,
            mcp_config=self._generate_mcp_config(manifest),
        )
```

输出目录结构：
```
<plugin-id>/
    .claude-plugin/
        plugin.json
    skills/
        <skill-id>/
            SKILL.md
            references/
    agents/
        <agent-id>.md
    .mcp.json
```

### Phase 4: Token 计量 + 配置增强（1-2 天）

#### 4.1 TokenMeter 增强

```python
class TokenMeter:
    def __init__(self, max_records: int = 10000, persist_path: str | None = None):
        self._max_records = max_records
        self._persist_path = persist_path  # None=纯内存, 路径=JSON 持久化

    async def measure(self, plugin_id, kind, ...) -> AsyncMeasureContext:
        """支持 async with 的上下文管理器"""
        return AsyncMeasureContext(self, plugin_id, kind, ...)

    def summary(self, since: float | None = None) -> dict:
        """带时间范围的汇总"""
        records = self._records_since(since) if since else self._records
        return self._aggregate(records)

    def prune(self) -> None:
        """淘汰超过 max_records 的旧记录"""
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]
```

#### 4.2 EcosystemConfig 增强

```python
@dataclass
class EcosystemConfig:
    # ... 现有字段 ...

    # 新增字段
    max_token_records: int = 10000
    token_persist_path: str | None = None
    sandbox_default_mode: str = "inline"  # inline | process
    mcp_transport: str = "stdio"          # stdio | sse | http
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 0                     # 0 = auto

    _observers: list[Callable] = field(default_factory=list, repr=False)

    def validate(self) -> list[str]:
        """返回验证错误列表"""
        errors = []
        if self.subagent_timeout_seconds <= 0:
            errors.append("subagent_timeout_seconds must be > 0")
        if self.max_auto_restart < 0:
            errors.append("max_auto_restart must be >= 0")
        return errors

    @classmethod
    def from_dict(cls, data: dict) -> tuple["EcosystemConfig", list[str]]:
        """安全解析，返回 (config, warnings)"""
        ...

    def notify_change(self, key: str, old, new) -> None:
        for obs in self._observers:
            obs(key, old, new)
```

### Phase 5: Desk Bridge 合并 + 清理（1 天）

#### 5.1 合并 DeskContext → DeskRuntime

```python
@dataclass
class DeskRuntime:
    """融合 fusion-desk 运行时，直接暴露给插件"""

    # 保留现有字段
    node_registry: Any | None = None
    task_scheduler: Any | None = None
    mlx_client: Any | None = None
    config_center: Any | None = None
    workflow_engine: Any | None = None
    desk_logger: logging.Logger | None = None

    # 从 DeskContext 合并
    mcp_gateway_port: int = 0
    registered_plugin_ids: set[str] = field(default_factory=set)

    # 新增
    _vram_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _vram_allocations: dict[str, int] = field(default_factory=dict, repr=False)
    _permissions: dict[str, list[str]] = field(default_factory=dict, repr=False)
    vram_total_mb: int = 0

    # 从 DeskContext 移入的方法（不再转发，直接实现）
    # acquire_vram, release_vram, log, check_file_permission, ...
```

#### 5.2 删除文件

- `desk_context.py` — 合并到 `desk_runtime.py`

#### 5.3 更新引用

所有 `DeskContext` → `DeskRuntime`：
- `registry.py`
- `lifecycle.py`
- `claude_gateway.py`
- `mcp_exporter.py`
- `token_meter.py`
- `builtin/caveman_compress.py`
- 所有测试文件

### Phase 6: 测试重建（2-3 天）

#### 6.1 测试分层

```
tests/
    unit/                       # 纯逻辑，无 I/O
        test_registry.py
        test_lifecycle.py
        test_config.py
        test_token_meter.py
        test_schema.py
    integration/                # 模块间交互
        test_mcp_handler.py     # JSON-RPC 处理器测试
        test_mcp_exporter.py
        test_claude_adapter.py
        test_claude_gateway.py
        test_sandbox.py
    e2e/                        # 端到端，真实 MCP 连接
        test_mcp_stdio.py       # 启动 stdio 服务器并连接
        test_mcp_sse.py         # SSE 传输
        test_plugin_lifecycle.py
```

#### 6.2 新增测试

| 测试 | 覆盖 |
|------|------|
| test_transport.py | StdioTransport/SSETransport 消息收发 |
| test_jsonrpc.py | JSON-RPC 请求解析、方法分发、错误处理 |
| test_mcp_handler.py | tools/list、tools/call、resources/*、prompts/* |
| test_sandbox.py | 进程隔离、超时强杀、IPC 通信 |
| test_skill_adapter.py | SKILL.md 生成、渐进式加载 |
| test_agent_adapter.py | agent .md 生成、model 选择 |
| test_plugin_bundle.py | 完整 plugin 目录结构生成 |
| test_concurrent.py | 并发 dispatch、vRAM 竞争、状态机并发 |

---

## 六、文件变更总览

### 新增文件

| 文件 | 说明 |
|------|------|
| `transport.py` | MCP 传输层（stdio/SSE/HTTP） |
| `jsonrpc.py` | MCP JSON-RPC 2.0 处理器 |
| `server.py` | MCP Server 入口 + CLI |
| `sandbox.py` | 插件沙箱（进程隔离） |
| `schema.py` | 共享 schema 定义（类型映射、MCP annotations） |
| `skill_adapter.py` | Claude Skill 生成器（替代 claude_adapter.py） |
| `agent_adapter.py` | Claude Agent 生成器 |
| `hook_adapter.py` | Hook 事件适配 |
| `plugin_bundle.py` | Claude Code Plugin 包生成器 |

### 删除文件

| 文件 | 原因 |
|------|------|
| `desk_context.py` | 合并到 desk_runtime.py |
| `claude_adapter.py` | 替换为 skill_adapter.py |

### 重构文件

| 文件 | 变更 |
|------|------|
| `registry.py` | frozen dataclass + 依赖声明 + ParamType enum + 自动发现 |
| `lifecycle.py` | 严格状态机 + per-plugin 配置 + 心跳修复 + 线程安全 |
| `claude_gateway.py` | 消除 manifest 篡改 + 使用新 adapter + MCP handler 集成 |
| `mcp_exporter.py` | 实现 call_tool + annotations + outputSchema + 协议版本 |
| `desk_runtime.py` | 合并 DeskContext + 线程安全 + 路径标准化 |
| `token_meter.py` | 修剪 + 持久化 + 时间范围 + 异步支持 |
| `config.py` | 验证 + 变更回调 + Desk 配置同步 |
| `builtin/caveman_compress.py` | 修复 URL 剥离 + 参数解析 + 移除 LONG_TASK |
| `__init__.py` | 更新导出列表 + 线程安全缓存 |
| `pyproject.toml` | 新增 CLI 入口 + mcp 依赖 |

---

## 七、依赖关系

```
Phase 0 (基础修复)
    │
    ├── Phase 1 (MCP 协议栈) ────── Phase 3 (Claude 适配)
    │                                      │
    ├── Phase 2 (插件沙箱)                 │
    │                                      │
    └── Phase 4 (计量+配置)                │
                                           │
    Phase 5 (Desk Bridge 合并) ────────────┘
                                           │
                                    Phase 6 (测试重建)
```

- Phase 1 和 Phase 2 可并行
- Phase 3 依赖 Phase 1（需要 MCP 工具描述增强）
- Phase 5 可与 Phase 1-4 并行
- Phase 6 在所有功能完成后执行

---

## 八、风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| fusion-desk 源码不存在，DeskRuntime 无法对接真实 API | 高 | 高 | 先基于 Protocol 接口 + mock 实现完整协议栈，等 fusion-desk 就绪后替换实现 |
| macOS sandbox-exec 在新版 macOS 可能被限制 | 中 | 中 | 降级为 subprocess + setrlimit，无 Apple Sandbox |
| MCP SDK Python v2 API 不稳定 | 低 | 中 | 先手写 JSON-RPC 处理器，后迁移到官方 SDK |
| 并行 Phase 可能产生合并冲突 | 中 | 低 | 每个 Phase 使用独立文件，减少交叉修改 |
| caveman 压缩修改可能影响已有用户 | 低 | 低 | 输出格式不变，仅修复 bug |

---

## 九、成功指标

| 指标 | 当前 | 目标 |
|------|------|------|
| Claude Desktop 可连接 | 否 | 是（stdio） |
| Claude Code 可调用 Skill | 否 | 是（SKILL.md） |
| 插件进程隔离 | 否 | 是（PROCESS 模式） |
| MCP 协议版本 | 2024-11-05 | 2026-07-28 |
| 测试覆盖 | 222 用例 | 300+ 用例（含 e2e） |
| 线程安全 | 否 | 是 |
| Token 计量持久化 | 否 | 可选 |
| 插件依赖声明 | 否 | 是 |
| 内置插件自动发现 | 硬编码 | 自动扫描 |
| A 级缺陷 | 7 | 0 |
| B 级缺陷 | 13 | 0 |

---

## 附录 A: MCP 协议 2026-07-28 关键变更

与当前硬编码的 `2024-11-05` 相比：

| 变更 | 说明 |
|------|------|
| Streamable HTTP 传输 | 替代旧 SSE 传输，单一 POST 端点 |
| server/discover | 能力协商，替代旧的 capabilities 字段 |
| resultType | 工具调用结果新增 `resultType: "complete" | "partial"` |
| structuredContent | 与 text content 并列的结构化输出 |
| outputSchema | 工具定义新增输出 schema |
| annotations | 工具定义新增行为注解（readOnlyHint 等） |
| icons | 工具定义新增图标 |
| 订阅/监听 | 资源变更推送机制 |
| MCP Registry | 集中式 MCP 服务器发现 |

## 附录 B: Claude Code Plugin 目录规范

```
plugin-name/
    .claude-plugin/
        plugin.json              # 必需 - 插件元数据
    commands/                    # 可选 - 斜杠命令 (.md)
    agents/                      # 可选 - 自动代理 (.md)
    skills/                      # 可选 - 技能包
        skill-name/
            SKILL.md             # 必需 - YAML frontmatter + markdown
            references/          # 可选 - 详细文档
            examples/            # 可选 - 代码示例
            scripts/             # 可选 - 可执行工具
            assets/              # 可选 - 输出资源
    hooks/                       # 可选 - 事件处理器
    .mcp.json                    # 可选 - 外部 MCP 服务器配置
    README.md                    # 可选 - 文档
```

### plugin.json 格式

```json
{
    "name": "plugin-name",
    "version": "1.0.0",
    "description": "Plugin description",
    "author": {"name": "Author", "email": "email"},
    "mcpServers": {
        "server-name": {
            "command": "${CLAUDE_PLUGIN_ROOT}/servers/server",
            "args": ["--port", "8080"],
            "env": {}
        }
    }
}
```

### SKILL.md 格式

```yaml
---
name: Skill Name
description: >
  This skill should be used when the user asks to "create X",
  "configure Y", "do Z". Include exact trigger phrases.
version: 0.1.0
---

# Skill instructions in imperative form...

## When to use
- Condition 1
- Condition 2

## How to use
Step-by-step instructions...

## Examples
Usage examples...
```

### Agent .md 格式

```yaml
---
name: agent-identifier
description: Use this agent when [conditions].
model: inherit|sonnet|opus|haiku
color: blue|cyan|green|yellow|magenta|red
tools: ["Read", "Write", "Grep", "Bash"]
---

You are [agent role]...

**Your Core Responsibilities:**
1. [Responsibility 1]
2. [Responsibility 2]

**Analysis Process:**
[Step-by-step workflow]
```

## 附录 C: MCP 工具定义完整 Schema (2026-07-28)

```json
{
    "name": "tool-name",
    "title": "Human-Readable Title",
    "description": "What this tool does",
    "inputSchema": {
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "Parameter description"
            }
        },
        "required": ["param1"]
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "result1": {"type": "string"}
        },
        "required": ["result1"]
    },
    "annotations": {
        "readOnlyHint": false,
        "destructiveHint": false,
        "idempotentHint": false,
        "openWorldHint": true
    },
    "icons": [
        {
            "src": "https://example.com/icon.png",
            "mimeType": "image/png",
            "sizes": ["48x48"]
        }
    ]
}
```

## 附录 D: MCP tools/call 请求/响应格式 (2026-07-28)

### 请求

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "tool-name",
        "arguments": {"param1": "value1"},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28"
        }
    }
}
```

### 响应（成功）

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "resultType": "complete",
        "content": [
            {"type": "text", "text": "Result text"}
        ],
        "structuredContent": {"result1": "value"},
        "isError": false
    }
}
```

### 响应（错误）

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "resultType": "complete",
        "content": [
            {"type": "text", "text": "Error description"}
        ],
        "isError": true
    }
}
```

## 附录 E: MCP Registry server.json 格式

```json
{
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.github.owner/server-name",
    "description": "Description of server",
    "repository": {
        "url": "https://github.com/owner/repo",
        "source": "github"
    },
    "version": "1.0.0",
    "packages": [
        {
            "registryType": "pypi",
            "identifier": "package-name",
            "version": "1.0.2",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {
                    "name": "API_KEY",
                    "isRequired": true,
                    "isSecret": true,
                    "description": "API key for service"
                }
            ]
        }
    ]
}
```
