# ex01 — Echo (Minimal Plugin)

最小可运行插件 / Minimal runnable plugin.

## 学到什么 / What you learn

- 插件 = 一个 callable `(desk, params) -> result` + 一个 `PluginManifest`
- `params.get(name, default)` 读参数
- `desk.log()` 写日志（不要用 `print`）
- `MCP_TOOL` + `CLAUDE_SKILL` 能力 → 自动暴露为 MCP 工具和 Claude Skill

## 代码要点 / Code highlights

`echo_plugin.py`:

```python
def echo(desk, params):
    text = params.get("text", "")
    if desk is not None:
        desk.log("echo", "INFO", "echoed", chars=len(text))
    return {"echo": text.upper()}
```

Manifest 关键字段:

```python
ECHO_MANIFEST = PluginManifest(
    id="echo",
    name="Echo Plugin",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL),
    params=(
        PluginParam(
            name="text",
            type=PluginParamType.STRING,
            description="Input text to echo.",
            required=True,
        ),
    ),
    entry_point=echo,
    timeout_seconds=30,
)
```

## 运行 / Run

```bash
cd /Users/dahai/fusion && source .venv/bin/activate
cd fusion-plugins-ecosystem
pytest examples/ex01_echo/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_echo_register_enable_execute` | 注册→启用→执行返回 `{"echo": "HELLO"}` |
| `test_mcp_tool_exposed` | MCP 工具名 `mcp__plugin__echo` 出现在工具列表 |
| `test_skill_exported` | Skill frontmatter `name: echo`，body 含 `## Parameters` 与 `**text**` |

## 关键约束 / Key constraints

- `id` 全局唯一，用作 MCP 工具名后缀
- `entry_point` 直接用 callable（INLINE 模式可用任何 callable）
- 返回值是普通 dict，lifecycle 原样返回
