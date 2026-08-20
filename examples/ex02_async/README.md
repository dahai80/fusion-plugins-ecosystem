# ex02 — Async Entry Point (异步入口)

异步插件入口 / Async plugin entry point.

## 学到什么 / What you learn

- `async def` 入口由 lifecycle 自动检测（`inspect.iscoroutinefunction`）
- 无需 `@pytest.mark.asyncio`（项目用 `asyncio_mode=auto`）
- `await asyncio.sleep(...)` 模拟异步等待

## 代码要点 / Code highlights

`async_plugin.py`:

```python
async def async_fetch(desk, params):
    label = params.get("label", "")
    delay = float(params.get("delay_seconds", 0.05))
    if desk is not None:
        desk.log("async_fetch", "INFO", "waiting", label=label, delay=delay)
    await asyncio.sleep(delay)
    return {"label": label, "waited": delay}
```

Manifest 与同步插件完全一致，只是 `entry_point` 指向 coroutine function:

```python
ASYNC_FETCH_MANIFEST = PluginManifest(
    id="async_fetch",
    ...
    params=(
        PluginParam(name="label", type=PluginParamType.STRING, required=True, ...),
        PluginParam(name="delay_seconds", type=PluginParamType.FLOAT,
                    required=False, default=0.05, ...),
    ),
    entry_point=async_fetch,  # async def — lifecycle 自动 await
)
```

## 运行 / Run

```bash
pytest examples/ex02_async/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_async_entry_point_is_coroutine` | `inspect.iscoroutinefunction(async_fetch) == True` |
| `test_async_fetch_executes` | 显式 `delay_seconds=0.02` → `{"label","waited":0.02}` |
| `test_async_fetch_default_delay` | 不传 delay → 默认 `0.05` |

## 关键点 / Key points

- async 检测发生在 `lifecycle._invoke_inline`（`inspect.iscoroutinefunction`）
- PROCESS 沙箱也支持 async 入口（worker 用 `asyncio.run`）
- 异步入口超时同样走 `asyncio.wait_for` 熔断
