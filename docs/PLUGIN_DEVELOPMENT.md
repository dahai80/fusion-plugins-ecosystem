# Plugin Development Guide

> How to write, register, and expose a plugin in `fusion-plugin-ecosystem`.

## 1. Minimal plugin

A plugin is a callable with signature `(desk: DeskContext, params: dict) -> result` plus a `PluginManifest`.

```python
# my_plugin.py
from fusion_plugins_ecosystem.registry import (
    PluginCapability, PluginCategory, PluginManifest, PluginParam,
)

def my_plugin(desk, params):
    """Echo plugin. Returns the input text uppercased."""
    text = params["text"]
    desk.log("my_plugin", "INFO", "echo called", chars=len(text))
    return {"echo": text.upper()}

MY_MANIFEST = PluginManifest(
    id="my_echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Echoes input text uppercased.",
    capabilities=[
        PluginCapability.MCP_TOOL,       # expose as MCP Tool
        PluginCapability.CLAUDE_SKILL,   # auto-convert to Claude Skill
    ],
    params=[
        PluginParam(name="text", type="string", description="Input text", required=True),
    ],
    entry_point=my_plugin,
    default_mounted=False,
    timeout_seconds=60,
    vram_mb=0,                          # 0 = no vRAM allocation
)
```

## 2. Register and run

```python
from fusion_plugins_ecosystem import PluginRegistry, PluginLifecycle, ClaudeGateway

registry = PluginRegistry()
registry.register(MY_MANIFEST)
registry.register_builtin()           # also load caveman_compress

lifecycle = PluginLifecycle(registry)
await lifecycle.enable("my_echo")
result = await lifecycle.execute("my_echo", {"text": "hello"})
assert result == {"echo": "HELLO"}
```

## 3. Auto-expose to Claude

No adapter code needed — the manifest drives everything.

```python
gw = ClaudeGateway(registry, lifecycle)

# Claude Skill (for Claude's tool catalog)
skills = gw.export_skills()
# [{"name":"my_echo","description":"Echoes input text uppercased.",
#   "input_schema":{"type":"object","properties":{"text":{"type":"string",...}},"required":["text"]},
#   "_fusion":{...}}]

# MCP Tool (for Claude Desktop / Claude Code MCP)
tools = gw.list_mcp_tools()
# [{"name":"my_echo","description":"...","inputSchema":{...}}]

# MCP tools/call
resp = await gw.invoke_mcp_tool("my_echo", {"text": "hello"})
# {"content":[{"type":"text","text":"{\"echo\":\"HELLO\"}"}],"isError":false}
```

## 4. Async plugin

Async entry points are auto-detected via `inspect.iscoroutinefunction`.

```python
async def my_async_plugin(desk, params):
    await desk.mlx_chat("qwen3.5", [{"role":"user","content":params["prompt"]}])
    return {"done": True}

MANIFEST = PluginManifest(
    id="my_async", ..., entry_point=my_async_plugin,
    capabilities=[PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL],
)
```

## 5. String entry point

For lazy-loaded plugins, use `"module:attr"` dotted path:

```python
PluginManifest(
    id="caveman_compress_lazy", ...,
    entry_point="fusion_plugins_ecosystem.builtin.caveman_compress:caveman_compress",
)
```

`PluginLifecycle.load` imports the module and resolves the attribute on first load.

## 6. Capabilities

Pick capabilities to control auto-exposure:

| Capability | Effect |
|------------|--------|
| `MCP_TOOL` | listed in `MCPExporter.list_tools` / `ClaudeGateway.list_mcp_tools` |
| `CLAUDE_SKILL` | exported by `ClaudeSkillAdapter.export_all` |
| `SUBAGENT` | listed in `ClaudeGateway.list_subagent_capable_plugins` (Claude Code subagent panel) |
| `FILE_ACCESS` | checked via `DeskContext.check_file_permission` (grant via `grant_permission`) |
| `VRAM_CONSUMER` | pair with `vram_mb>0` to acquire vRAM via `DeskContext.acquire_vram` |
| `LONG_TASK` | pair with `timeout_seconds` to enable meltdown + auto-restart |

```python
PluginManifest(
    id="mlx_vision", ...,
    capabilities=[
        PluginCapability.MCP_TOOL,
        PluginCapability.CLAUDE_SKILL,
        PluginCapability.VRAM_CONSUMER,
        PluginCapability.LONG_TASK,
    ],
    vram_mb=4096,
    timeout_seconds=300,
)
```

## 7. Params schema

`PluginParam` maps to JSON Schema in both Claude Skill `input_schema` and MCP `inputSchema`:

| `type` field | JSON Schema type |
|--------------|------------------|
| `string` | `string` |
| `int` | `integer` |
| `bool` | `boolean` |
| `array` | `array` |
| `object` | `object` |
| `float` | `number` |

```python
params=[
    PluginParam(name="mode", type="string", description="Mode",
                enum=["fast", "slow"], default="fast"),
    PluginParam(name="count", type="int", description="Count", default=10),
    PluginParam(name="enabled", type="bool", description="Enabled"),
]
# → input_schema.properties.mode = {"type":"string","enum":["fast","slow"],"default":"fast"}
# → input_schema.properties.count = {"type":"integer","default":10}
# → input_schema.properties.enabled = {"type":"boolean"}
```

## 8. Default mounting

Set `default_mounted=True` to auto-mount the plugin to every Claude session (caveman uses this).

```python
registry.default_mounted()             # → list of default_mounted manifests
gw.export_default_mounted_skills()     # → their Claude Skill dicts
```

Controlled by `config.default_mount_compressor`.

## 9. Token metering

Use `TokenMeter.measure` to record consumption per kind. The gateway already wraps `invoke_mcp_tool` (→ `MCP_RELAY`) and `dispatch_subagent` (→ `PLUGIN_LOCAL`); only instrument internal plugin logic if needed.

```python
from fusion_plugins_ecosystem import TokenKind

def my_plugin(desk, params):
    with desk.runtime.config_center as _:  # example
        pass
    # For plugins doing MLX calls:
    # TokenMeter is injected via the gateway; manual use:
    return {"ok": True}
```

The meter warns on `PLUGIN_LOCAL ∧ total_tokens=0 ∧ wall_seconds>60` (the "stuck subagent" signal).

## 10. Logging

Always log via `desk.log`, never via module-level `print`/`logging` directly — this feeds the fusion-desk full-chain panel.

```python
def my_plugin(desk, params):
    desk.log("my_plugin", "INFO", "started", input_size=len(params["text"]))
    ...
    desk.log("my_plugin", "ERROR", "failed", reason="oom")
```

## 11. Permissions

For plugins touching the filesystem, declare `FILE_ACCESS` and request paths:

```python
desk.grant_permission("my_plugin", ["/data/", "/tmp/"])
assert desk.check_file_permission("my_plugin", "/data/file.txt") is True
assert desk.check_file_permission("my_plugin", "/etc/passwd") is False
```

Empty allowlist = allow all (useful for tests).

## 12. Testing your plugin

```python
import pytest
from fusion_plugins_ecosystem import PluginRegistry, PluginLifecycle, ClaudeGateway
from my_plugin import MY_MANIFEST

async def test_my_plugin_via_mcp():
    registry = PluginRegistry()
    registry.register(MY_MANIFEST)
    gw = ClaudeGateway(registry, PluginLifecycle(registry))
    resp = await gw.invoke_mcp_tool("my_echo", {"text": "hi"})
    assert resp["isError"] is False
    import json
    assert json.loads(resp["content"][0]["text"]) == {"echo": "HI"}

def test_my_plugin_skill_export():
    registry = PluginRegistry()
    registry.register(MY_MANIFEST)
    gw = ClaudeGateway(registry)
    skills = gw.export_skills()
    assert skills[0]["name"] == "my_echo"
    assert "text" in skills[0]["input_schema"]["required"]
```

## 13. Checklist before publishing

- [ ] `id` globally unique
- [ ] `description` one-liner (drives Claude's tool-use decision)
- [ ] `params` schema complete with `required` flags
- [ ] `capabilities` minimal (don't claim `VRAM_CONSUMER` if `vram_mb=0`)
- [ ] `timeout_seconds` set for any `LONG_TASK`
- [ ] All logs via `desk.log`, never `print`
- [ ] No direct `fusion_desk` internal imports — go through `DeskContext`
- [ ] Tests cover: MCP invoke, Skill export, edge cases (empty input, crash, timeout)
