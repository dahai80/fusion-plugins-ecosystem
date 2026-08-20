# ex08 — Process Sandbox (进程隔离)

`SandboxMode.PROCESS` 把入口放到子进程执行（`PluginSandbox`），崩溃隔离 / Entry point runs in a subprocess over JSON IPC, crash-isolated.

## 学到什么 / What you learn

- `SandboxMode.PROCESS` + `entry_point` 必须是**模块级可导入属性**
- 入口用 `"module:attr"` 字符串形式（子进程 `importlib.import_module` 解析）
- 闭包、lambda、`<locals>` 函数**不能**用于 PROCESS 模式 —— 导入会失败
- 结果经 stdin/stdout JSON IPC 回传
- PROCESS 模式**不能**申请 VRAM（抛 `NotImplementedError`）

## 代码要点 / Code highlights

`process_plugin.py` 入口是模块级函数:

```python
def isolated_worker(desk, params):
    value = params.get("value", "")
    if desk is not None:
        desk.log("isolated_worker", "INFO", "ran in subprocess", value=value)
    return {"processed": value[::-1], "pid_side": "subprocess"}
```

manifest 用字符串指向它:

```python
ISOLATED_WORKER_MANIFEST = PluginManifest(
    id="isolated_worker",
    capabilities=(PluginCapability.MCP_TOOL,),
    entry_point="examples.ex08_process_sandbox.process_plugin:isolated_worker",
    timeout_seconds=60,
    sandbox_mode=SandboxMode.PROCESS,
)
```

## 运行 / Run

```bash
pytest examples/ex08_process_sandbox/ -v
```

需项目根在 `sys.path` 上，子进程才能 `import examples.ex08_process_sandbox.process_plugin`。

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_process_sandbox_executes_in_subprocess` | `value="hello"` → `{"processed":"olleh","pid_side":"subprocess"}` |
| `test_process_sandbox_empty_value` | `value=""` → `{"processed":"","pid_side":"subprocess"}` |

## 关键点 / Key points

- `pid_side:"subprocess"` 证明执行在子进程内，非主进程
- 入口必须是模块顶层定义 —— `qualname` 含 `<locals>` 会 spawn 失败
- 生产环境进程托管由 fusion-cowork 的 sandbox 提供；本包内 `PluginSandbox` 供 standalone `fusion-plugin-server` 测试与 PROCESS manifest 路径
- 需要 VRAM 的插件用 INLINE，不用 PROCESS
