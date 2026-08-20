# ex04 — Subagent Dispatch (反向子代理调度)

fusion 主动拉起 Claude Code 子代理 / fusion pulls a Claude Code subagent.

## 学到什么 / What you learn

- `SUBAGENT` 能力 → 出现在 `ClaudeGateway.list_subagent_capable_plugins()`
- 反向调度: `ClaudeGateway.dispatch_subagent(SubagentTask(...))`
- 返回结构: `{"task", "plugin_id", "state":"completed"|"failed", "result"|"error"}`
- `agent_model` 字段指定子代理使用的模型（如 `claude-fable-5`）
- 未注册插件 → `dispatch_subagent` 抛 `KeyError`（注册校验在执行前）
- 执行异常 → 被 try/except 捕获，返回 `state:"failed"`

## 代码要点 / Code highlights

`subagent_plugin.py` manifest:

```python
BATCH_REFACTOR_MANIFEST = PluginManifest(
    id="batch_refactor",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.SUBAGENT),
    category=PluginCategory.CODING_PLAN,
    params=(
        PluginParam(name="target", type=PluginParamType.STRING, required=True, ...),
        PluginParam(name="action", type=PluginParamType.STRING,
                    default="rename", enum=("rename", "extract", "inline"), ...),
    ),
    agent_model="claude-fable-5",
    timeout_seconds=120,
    ...
)
```

调度调用:

```python
task = SubagentTask(
    name="refactor-1",
    plugin_id="batch_refactor",
    arguments={"target": "old_name", "action": "rename"},
    timeout_seconds=30,
)
result = await gw.dispatch_subagent(task)
# result["state"] == "completed", result["result"] == {...}
```

## 运行 / Run

```bash
pytest examples/ex04_subagent/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_subagent_capability_listed` | `"batch_refactor"` 在 `list_subagent_capable_plugins()` |
| `test_dispatch_subagent_completes` | `state:"completed"`，`result` 含 `refactored`/`action`/`files_touched` |
| `test_dispatch_subagent_unknown_plugin_raises` | 未知 plugin_id → 抛 `KeyError`，含插件名 |
| `test_dispatch_subagent_propagates_execution_error` | 入口抛异常 → `state:"failed"`，`error` 含异常信息 |

## 关键点 / Key points

- `dispatch_subagent` 会自动 `enable` 插件再执行
- `config.subagent_timeout_destroy` 控制超时后是否自动 unload
- `timeout` 优先级: `task.timeout_seconds` > manifest > config
