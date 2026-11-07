---
doc_id: fusion-plugins-ecosystem/recipes/write-a-plugin
product: fusion-plugins-ecosystem
class: platform
entry: developer
doc_type: recipe
title: {zh: "配方：编写一个新插件", en: "Recipe: Write a New Plugin"}
audience: developer
status: draft
version: 0.8.0
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - docs/PLUGIN_DEVELOPMENT.md
  - examples/ex01_echo
---

# 配方：编写一个新插件 / Recipe: Write a New Plugin

目标：从零写一个可被 MCP / Claude 自动发现的插件并验证。参考实现：`examples/ex01_echo/`。

Goal: write a plugin from zero that MCP / Claude auto-discovers, then verify it. Reference: `examples/ex01_echo/`.

## 前置 / Prerequisites

```bash
source /Users/dahai/fusion/.venv/bin/activate
cd /Users/dahai/fusion/fusion-plugins-ecosystem
pip install -e ".[test]"    # fusion-cowork already installed if imports pass
```

## 步骤 / Steps

### 1. 声明 manifest（新建 `my_plugins/greeter.py`）

manifest 是冻结 dataclass；能力声明决定暴露形态 / the manifest is a frozen dataclass; capabilities decide exposure:

```python
import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.registry import (
    PluginCapability, PluginCategory, PluginManifest, PluginParam,
)

def greeter(desk, params):
    desk.log("greeter", "INFO", "called", name=params["name"])
    return {"greeting": f"hello {params['name']}"}
GREETER_MANIFEST = PluginManifest(
    id="greeter", name="Greeter", version="0.1.0",
    category=PluginCategory.CUSTOM, description="Returns a greeting.",
    capabilities=[PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL],
    params=[PluginParam(name="name", type="string", description="Who", required=True)],
    entry_point=greeter, timeout_seconds=60,
)
```

### 2. 注册、执行、验证自动暴露 / Register, execute, verify exposure

```python
registry = fpe.PluginRegistry()
registry.register_builtin()          # optional built-in compressor
registry.register(GREETER_MANIFEST)
lifecycle = fpe.PluginLifecycle(registry)

import asyncio
print(asyncio.run(lifecycle.execute("greeter", {"name": "fusion"})))
# → {"greeting": "hello fusion"}

gw = fpe.ClaudeGateway(registry, lifecycle)
assert any(t["name"] == "greeter" for t in gw.list_mcp_tools())
```

### 3. 验证 / Verify

仿照 `examples/ex01_echo/test_echo.py` 写测试（manifest + execute + 暴露断言），文件放 `tests/`。/ Follow `examples/ex01_echo/test_echo.py` for test assertions; put files under `tests/`.

```bash
pytest tests/ -q
```

`execute("greeter", {...})` 返回预期 dict、`list_mcp_tools()` 包含 `greeter` 即为完成。/ Done when the execute result matches and `list_mcp_tools()` contains `greeter`.

## 常见坑 / Common pitfalls

- **manifest 注册后不可变**（frozen dataclass）—— 改字段需重建 manifest；id 全局唯一，与 `caveman_compress` 等冲突会失败。/ Manifests are frozen after registration; ids must be globally unique.
- **entry_point 签名固定 `(desk, params)`** —— desk 是 DeskRuntime，不要加参数。/ The entry-point signature is fixed at `(desk, params)`.
- **异步入口直接 `async def`**（见 `examples/ex02_async/`），不要在同步入口里自建事件循环。/ Async entries are plain `async def` (see `examples/ex02_async/`).
- **文件访问**需声明 `FILE_ACCESS` 能力并经 `desk.check_file_permission()` 门控（见 `examples/ex09_file_access/`）。/ File access requires the `FILE_ACCESS` capability and permission gating (see `examples/ex09_file_access/`).
- **长任务**声明 `LONG_TASK` 并调大 `timeout_seconds`，否则触发超时熔断。/ Declare `LONG_TASK` and raise `timeout_seconds` for long tasks, or meltdown kicks in.
