---
doc_id: fusion-plugins-ecosystem/get-started
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: get-started
title: {zh: "快速开始", en: "Get Started"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - docs/PLUGIN_DEVELOPMENT.md
  - pyproject.toml
---

# 快速开始 / Get Started

面向要在 fusion-cowork 之上开发插件的开发者。本文所有命令均来自仓库实际代码。

For developers writing plugins on top of fusion-cowork. Every command below comes from the actual repo.

## 1. 安装 / Install

依赖 fusion-cowork 与 fusion-core（均不在 PyPI，需从仓库安装）。fusion-core 在共享 venv 中通常已就位。

fusion-cowork and fusion-core are not on PyPI — install them from their repos. fusion-core is usually already present in the shared venv.

```bash
cd /Users/dahai/fusion
source .venv/bin/activate

cd fusion-plugins-ecosystem
pip install -e "../fusion-cowork"    # base runtime (not on PyPI)
pip install -e ".[test]"             # this package + test deps
```

## 2. 验证 / Verify

```bash
pytest tests/ -q
```

747 tests collected as of 2026-11-07 (`pytest --collect-only -q | tail -1`)。示例插件另有独立测试：`pytest examples/`。

747 tests collected as of 2026-11-07 (`pytest --collect-only -q | tail -1`). Example plugins ship their own tests: `pytest examples/`.

## 3. 30 秒写一个插件 / A 30-second plugin

```python
import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.registry import (
    PluginCapability, PluginCategory, PluginManifest, PluginParam,
)

def my_plugin(desk, params):
    desk.log("my_plugin", "INFO", "called")
    return {"echo": params["text"].upper()}

MY_MANIFEST = PluginManifest(
    id="my_echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Echoes input text uppercased.",
    capabilities=[PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL],
    params=[PluginParam(name="text", type="string", description="Input", required=True)],
    entry_point=my_plugin,
    timeout_seconds=60,
)

registry = fpe.PluginRegistry()
registry.register_builtin()   # built-in caveman compressor
registry.register(MY_MANIFEST)

lifecycle = fpe.PluginLifecycle(registry)
await lifecycle.execute("my_echo", {"text": "hello"})
```

注册一次，插件即自动出现在 MCP `tools/list` 与 Claude Skill 目录 —— 无需任何适配代码。完整字段说明见 [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md)。

Register once and the plugin auto-appears in MCP `tools/list` and the Claude Skill catalog — zero adapter code. Full field reference: [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md).

## 4. 独立 MCP server / Standalone MCP server

`fusion-plugin-server`（注册在 `[project.scripts]`，实现在 `server.py`）用于在 fusion-cowork 之外测试 MCP 线路协议：

`fusion-plugin-server` (registered in `[project.scripts]`, implemented in `server.py`) tests the MCP wire protocol outside fusion-cowork:

```bash
fusion-plugin-server --transport stdio               # Claude Desktop subprocess mode
fusion-plugin-server --transport sse   --port 8765   # SSE client
fusion-plugin-server --transport http  --port 8765   # Streamable HTTP (MCP 2026-07-28)
```

## 5. Extensions 控制台 / Extensions console

```bash
pip install -e ".[console]"
fusion-plugin-console --host 127.0.0.1 --port 8795
```

打开 `http://127.0.0.1:8795`：Connectors / Plugins·MCP / Skills / Sub-agents 四个标签页。

Open `http://127.0.0.1:8795`: the Connectors / Plugins·MCP / Skills / Sub-agents tabs.

## 6. 下一步 / Next steps

- [recipes/write-a-plugin.md](recipes/write-a-plugin.md) — 完整插件编写流程 / full plugin authoring flow
- [recipes/expose-to-claude-mcp.md](recipes/expose-to-claude-mcp.md) — 把插件暴露给 Claude / expose plugins to Claude
- [recipes/install-connector-pack.md](recipes/install-connector-pack.md) — 安装外部连接器 / install external connectors
- [api-reference.md](api-reference.md) / [config-reference.md](config-reference.md) / [architecture.md](architecture.md)
