---
doc_id: fusion-plugins-ecosystem/recipes/expose-to-claude-mcp
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: recipe
title: {zh: "配方：把插件暴露给 Claude（MCP）", en: "Recipe: Expose Plugins to Claude via MCP"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - fusion_plugins_ecosystem/server.py
  - fusion_plugins_ecosystem/claude_gateway.py
---

# 配方：把插件暴露给 Claude（MCP）/ Recipe: Expose Plugins to Claude via MCP

目标：用独立 MCP server 暴露插件目录，验证 Claude Desktop 与 VS Code Claude Code 两条接入路径。

Goal: expose the plugin catalog via the standalone MCP server and verify the Claude Desktop and VS Code Claude Code access paths.

## 前置 / Prerequisites

```bash
source /Users/dahai/fusion/.venv/bin/activate && cd /Users/dahai/fusion/fusion-plugins-ecosystem && python -c "import fusion_plugins_ecosystem"
```

## 步骤 / Steps

### 1. 选传输方式 / Pick a transport

```bash
fusion-plugin-server --transport stdio                 # Claude Desktop subprocess mode
fusion-plugin-server --transport sse   --port 8765     # SSE client
fusion-plugin-server --transport http  --port 8765     # Streamable HTTP (MCP 2026-07-28)
```

stdio 供 Claude Desktop 子进程拉起；SSE/HTTP 供远程客户端（Web Claude 中继）。端口默认 0 = auto，env `FUSION_PLUGINS_ECOSYSTEM_PORT` 可固定。

stdio is spawned by Claude Desktop as a subprocess; SSE/HTTP serve remote clients (Web Claude relay). Port 0 (default) = auto; pin with `FUSION_PLUGINS_ECOSYSTEM_PORT`.

### 2. 进程内冒烟 / In-process smoke test

```python
import asyncio
import fusion_plugins_ecosystem as fpe
registry = fpe.PluginRegistry()
registry.register_builtin()
lifecycle = fpe.PluginLifecycle(registry)
gw = fpe.ClaudeGateway(registry, lifecycle)

print([t["name"] for t in gw.list_mcp_tools()])   # caveman_compress listed
resp = asyncio.run(gw.invoke_mcp_tool("caveman_compress", {"text": "# comment\ncode"}))
assert resp["isError"] is False
```

### 3. 配置 Claude Desktop / Configure Claude Desktop

在 Claude Desktop 的 MCP 配置中把命令指向 `fusion-plugin-server --transport stdio`（`gateway_info` + `list_mcp_tools` 路径）。

Point Claude Desktop's MCP config at `fusion-plugin-server --transport stdio` (`gateway_info` + `list_mcp_tools` path).

### 4. 反向子代理（可选）/ Reverse subagent (optional)

VS Code Claude Code 插件经 `dispatch_subagent` 调用声明了 `subagent` 能力的插件，返回 `{"state": "completed", "result": ...}`：

The VS Code Claude Code plugin reaches `subagent`-capable plugins via `dispatch_subagent`, returning `{"state": "completed", "result": ...}`:

```python
task = fpe.SubagentTask(name="compress", plugin_id="caveman_compress",
                        arguments={"text": "a\n\n\n\nb"})
result = asyncio.run(gw.dispatch_subagent(task))
assert result["state"] == "completed"
```

## 验证 / Verification

`gw.list_mcp_tools()` 包含预期插件、`invoke_mcp_tool(...)` 返回 `isError: false`、Claude Desktop 会话内可见并列出 MCP 工具。/ Expected plugins listed, `invoke_mcp_tool(...)` returns `isError: false`, tools visible in a Claude Desktop session.

## 常见坑 / Common pitfalls

- **Claude Desktop 拉不起 stdio server**：确认命令在该环境可解析（venv 内注册的 script）。/ If the stdio server cannot spawn, confirm the command resolves there (venv script).
- **端口被占**：8765 属 port-registry 固定端口；auto 模式客户端应从 `gateway_info()` 拿实际地址。/ Port 8765 is registry-fixed; auto-mode clients should read the real address from `gateway_info()`.
- **工具没出现**：manifest 未声明 `MCP_TOOL`，或 `EcosystemConfig.enable_claude_mcp` 已关。/ Missing tool: capability not declared, or `enable_claude_mcp` is off.
- **子代理超时**：默认 600 秒自动销毁；长任务调 `subagent_timeout_seconds` 或声明 `LONG_TASK`。/ Subagents are destroyed after 600 s by default; raise the timeout or declare `LONG_TASK`.
- **凭证类插件**：Volcengine Coding Plan 走 `gw.store_credentials("volcengine_claude", ...)`，凭证只存本地 Fernet。/ Volcengine Coding Plan uses `store_credentials`; credentials stay in the local Fernet store.
