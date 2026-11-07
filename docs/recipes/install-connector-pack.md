---
doc_id: fusion-plugins-ecosystem/recipes/install-connector-pack
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: recipe
title: {zh: "配方：安装连接器 pack 并审批高风险工具", en: "Recipe: Install a Connector Pack and Approve HIGH-Risk Tools"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - fusion_plugins_ecosystem/connector.py
  - fusion_plugins_ecosystem/connector_packs.py
  - fusion_plugins_ecosystem/marketplace.py
---

# 配方：安装连接器 pack 并审批高风险工具 / Recipe: Install a Connector Pack and Approve HIGH-Risk Tools

目标：安装第一方连接器 pack（以 Feishu 为例），桥接工具、审批高风险工具并执行。

Goal: install a first-party connector pack (Feishu here), bridge its tools, approve HIGH-risk tools, and execute.

## 前置 / Prerequisites

```bash
source /Users/dahai/fusion/.venv/bin/activate && cd /Users/dahai/fusion/fusion-plugins-ecosystem && python -c "from fusion_plugins_ecosystem.connector_packs import get_builtin_pack"
```

第一方 pack：Feishu（官方 lark-mcp）、DingTalk、WeCom、Mail（IMAP/SMTP）、Calendar（CalDAV）、Notion（官方 @notionhq/notion-mcp-server）。

First-party packs: Feishu (official lark-mcp), DingTalk, WeCom, Mail (IMAP/SMTP), Calendar (CalDAV), Notion (official @notionhq/notion-mcp-server).

## 步骤 / Steps

### 1. 安装 pack 并配置凭证 / Install and set credentials

pack 是纯数据（transport/command/url + 凭证 provider + 风险级）；安装即桥接为 `connector__feishu__<tool>` 能力插件。/ A pack is pure data; install bridges tools as `connector__feishu__<tool>` capability plugins.

凭证从 `DeskRuntime.get_api_key`（Fernet）解析：stdio 注入 env、HTTP 注入 Authorization，只存本地。/ Credentials resolve from `DeskRuntime.get_api_key` (Fernet): stdio injects env vars, HTTP injects the Authorization header, local-only.

```python
import asyncio
import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.connector_packs import get_builtin_pack

desk = fpe.DeskRuntime()
registry = fpe.PluginRegistry(desk)
creg = fpe.ConnectorRegistry(
    desk=desk, registry=registry, persist_path="~/.fusion-ecosystem/connectors.json"
)
creg.install_pack(get_builtin_pack("feishu"))
desk.set_api_key("feishu", "<your-key-from-feishu-open-platform>")
```

### 2. 审批高风险工具 / Approve HIGH-risk tools

未审批的 HIGH 工具执行时抛 `PermissionError`；只读工具按约定为 LOW，无需审批（测试强制）。

Unapproved HIGH tools raise `PermissionError`; read-only tools are LOW by convention (test-enforced).

```python
creg.approve_tool("feishu", "im.message.send")
```

### 3. 执行 / Execute

调用经外部 MCP server 路由；连接器工具固定 INLINE（外部 server 已是隔离进程）。/ Calls route through the external MCP server; connector tools run INLINE.

```python
lifecycle = fpe.PluginLifecycle(registry)
resp = asyncio.run(lifecycle.execute(
    "connector__feishu__im.message.send",
    {"chat_id": "oc_xxx", "content": "hello"},
))
```

## 验证 / Verification

`market.list_installed()` 包含 `feishu`、注册表存在 `connector__feishu__*` 插件、未审批 HIGH 工具抛 `PermissionError` 且审批后返回 MCP content。/ feishu installed, bridge plugins registered, unapproved HIGH tools raise, approved ones return MCP content.

## 常见坑 / Common pitfalls

- **`sandbox_default_mode="process"` 时安装被拒**：连接器只允许 INLINE。/ Install is refused when the default sandbox mode is process — connector tools run INLINE only.
- **远程索引必须 HTTPS**（测试强制）；本地索引用 JSON 文件路径。/ Remote indexes must be HTTPS (test-enforced); local indexes take a JSON path.
- **凭证未配置**：连接阶段 provider 解析失败，先 `desk.set_api_key(...)`。/ Missing credentials fail at connect time — set them first.
- **WRITE 通道不自动暴露**：控制台安装的 REST 连接器 WRITE 通道进人工审核队列，仅 READ（GET）自动桥接。/ WRITE channels of console-installed REST connectors enter human review; only READ (GET) auto-bridges.
- **id 冲突**：市场索引条目按 id 覆盖内置目录，确认预期后再安装；也可经 `Marketplace(creg, marketplace_index=...)` 安装。/ Marketplace index entries override builtin entries on id collision; packs can also install via `Marketplace(creg, marketplace_index=...)`.
