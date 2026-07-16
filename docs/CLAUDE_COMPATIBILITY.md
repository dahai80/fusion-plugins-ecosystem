# Claude Compatibility

> How `fusion-plugin-ecosystem` natively supports Claude across all three layers — Claude Desktop, Claude Code, and the MCP protocol — with zero extra adapter code.

## 1. Three-layer compatibility

| Access | Constant | Gateway entry |
|--------|----------|---------------|
| Claude Desktop client | `CLAUDE_DESKTOP` | `list_mcp_tools` + `gateway_info` (stdio transport) |
| VS Code Claude Code plugin | `CLAUDE_CODE` | `dispatch_subagent` + `list_subagent_capable_plugins` |
| Web Claude | `CLAUDE_WEB` | `invoke_mcp_tool` (HTTP/SSE relay via fusion-desk) |
| Volcengine Claude Coding Plan | `CLAUDE_VOLCENGINE` | `store_credentials("volcengine_claude", ...)` |

```python
from fusion_plugins_ecosystem import ClaudeGateway, CLAUDE_DESKTOP, CLAUDE_CODE

gw = ClaudeGateway(registry, lifecycle)
# Claude Desktop: MCP tools/list
tools = gw.list_mcp_tools()
# Claude Code: subagent dispatch
await gw.dispatch_subagent(task)
```

## 2. Forward: Claude calls fusion

Every plugin with `CLAUDE_SKILL` capability auto-converts to a Claude Skill dict; every plugin with `MCP_TOOL` capability auto-registers as an MCP Tool. No per-plugin adapter code.

```python
skills = gw.export_skills()        # → feed to Claude's tool catalog
tools = gw.list_mcp_tools()        # → MCP tools/list response
resp = await gw.invoke_mcp_tool("caveman_compress", {"text": "..."})
# resp = {"content": [{"type":"text","text":"{...json...}"}], "isError": False}
```

The MCP response follows the standard `tools/call` shape: `content` is a list of `{type, text}` items; `isError` flags failures. Dict results are JSON-serialized; other types are `str()`-ified.

## 3. Reverse: fusion pulls Claude Code subagent

fusion-desk can proactively spin up a Claude Code subagent to run long tasks — batch refactors, PR generation, code optimization.

```python
from fusion_plugins_ecosystem import SubagentTask

task = SubagentTask(
    name="batch-refactor",
    plugin_id="code_refactor",
    arguments={"repo": "/path", "files": ["a.py", "b.py"]},
    timeout_seconds=300,
)
result = await gw.dispatch_subagent(task)
# result = {"task":"batch-refactor","plugin_id":"code_refactor","state":"completed","result":{...}}
```

On timeout/crash:
- `config.subagent_timeout_destroy=True` (default) → plugin instance is `unload()`-ed (auto-destroy)
- `config.subagent_timeout_destroy=False` → instance preserved (state `TIMEOUT`/`CRASHED`) for inspection

`manifest.timeout_seconds` is temporarily overridden for the task duration and restored in the `finally` block.

## 4. Volcengine Claude Coding Plan auth

Credentials are stored in the fusion-desk config center, keyed by provider. The volcengine provider is gated by `enable_volcengine_claude_plan`.

```python
gw.store_credentials("volcengine_claude", "sk-vol-xxx")
gw.store_credentials("anthropic", "sk-ant-yyy")

assert gw.has_credentials("volcengine_claude") is True
key = gw.get_credentials("volcengine_claude")   # → "sk-vol-xxx"

# Disable volcengine plan
config.enable_volcengine_claude_plan = False
gw.config = config
gw.store_credentials("volcengine_claude", "sk-vol-xxx")  # refused, not stored
gw.get_credentials("volcengine_claude")                   # → None
```

## 5. fusion-mlx as Claude visual backend

fusion-mlx local inference serves as Claude's visual/image generation backend, gated by `enable_mixed_quantization`.

```python
result = await gw.mlx_visual_backend(
    model="qwen3.5-vl",
    messages=[{"role":"user","content":"describe this image"}],
)
```

Token consumption is recorded under `TokenKind.MLX_INFERENCE`.

## 6. Full-chain token metering

`TokenMeter` splits consumption by kind, solving the "subagent runs 40 min with zero token consumption" pain point:

| `TokenKind` | When |
|-------------|------|
| `CLAUDE_MODEL` | Claude model inference (by Claude API) |
| `PLUGIN_LOCAL` | plugin local compute (wall-clock + MLX token) |
| `MLX_INFERENCE` | fusion-mlx local inference |
| `MCP_RELAY` | MCP protocol relay overhead |

```python
meter = gw.token_meter
with meter.measure("caveman", TokenKind.PLUGIN_LOCAL):
    ...  # plugin runs
# auto-records wall_seconds; warns if total=0 ∧ wall>60
print(meter.summary())   # {"caveman": {"plugin_local": 0, "mcp_relay": 120}}
```

## 7. Config toggles (one-click panel)

All Claude capabilities are toggleable via `EcosystemConfig`, defaulting to **all-on** for native compatibility.

| Toggle | Controls | Off behavior |
|--------|----------|--------------|
| `enable_claude_mcp` | `list_mcp_tools`, `invoke_mcp_tool` | returns `[]` / `isError=True` |
| `auto_export_claude_skill` | `export_skills` | returns `[]` |
| `default_mount_compressor` | `export_default_mounted_skills` | returns `[]` |
| `subagent_timeout_destroy` | `dispatch_subagent` | no unload on timeout |
| `enable_volcengine_claude_plan` | volcengine credential store/get | refused / returns `None` |
| `enable_mixed_quantization` | `mlx_visual_backend` | raises `RuntimeError` |

## 8. Unified logging

All plugin logs route through `DeskContext.log(plugin_id, level, message, **kw)` → `DeskRuntime.log`, which feeds the fusion-desk full-chain log panel. This solves the "subagent no logs" pain point — every `invoke_mcp_tool`, `dispatch_subagent`, lifecycle transition, and credential operation is logged with `[plugin=<id>]` prefix.

## 9. Mac M-series native

- Native support for Claude web, Claude Desktop client, VS Code Claude Code plugin
- Native compatibility with Claude subagent loops, long tasks, multi-turn tool calls — `PluginLifecycle` provides timeout meltdown + process restart
- fusion-mlx Metal/MLX acceleration for local inference backend
