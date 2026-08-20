# ex07 — Long Task (超时熔断 + 自动重启)

`LONG_TASK` 能力 + 短超时。超时后插件转 `TIMEOUT`，`_maybe_restart` 重载它（上限 `max_restart`）/ Timeout meltdown then auto-restart up to max_restart.

## 学到什么 / What you learn

- `LONG_TASK` 能力 + manifest `timeout_seconds` 与 `max_restart`
- 超时抛 `asyncio.TimeoutError`，插件状态转 `TIMEOUT`
- `_maybe_restart` 自动 `unload→load→enable`，`restart_count` 递增，状态回 `ENABLED`
- 重启后插件仍可执行
- 触发超时用 `execute(..., timeout_override=)` 覆盖超时阈值

## 代码要点 / Code highlights

`long_task_plugin.py` 入口模拟耗时工作:

```python
async def slow_worker(desk, params):
    work_seconds = float(params.get("work_seconds", 0.1))
    if desk is not None:
        desk.log("slow_worker", "INFO", "starting", work_seconds=work_seconds)
    await asyncio.sleep(work_seconds)
    return {"done": True, "worked": work_seconds}
```

manifest 声明长任务 + 重启上限:

```python
SLOW_WORKER_MANIFEST = PluginManifest(
    id="slow_worker",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.LONG_TASK),
    entry_point=slow_worker,
    timeout_seconds=60,
    max_restart=3,
)
```

## 运行 / Run

```bash
pytest examples/ex07_long_task/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_slow_worker_normal_execute` | 正常执行 `work_seconds=0.02` 返回 `{"done":True,"worked":0.02}` |
| `test_timeout_triggers_meltdown_and_restart` | `work_seconds=0.3` 超 `timeout_override=0.05` → 抛 `TimeoutError`；之后 `restart_count==1`、`state==ENABLED`；再执行仍成功 |

## 关键点 / Key points

- 重启阈值：`max_restart`（manifest）与全局 `MAX_RESTART=3`，取约束值
- 重启 = `unload→load→enable`，**不是** `reload()`（lifecycle 无公开 `reload`，内部走私有 `_maybe_restart`）
- 达 `max_restart` 后不再重启，插件留在 `TIMEOUT` 状态
- 超时后捕获的是 `asyncio.TimeoutError`，调用方须处理
