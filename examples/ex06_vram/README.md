# ex06 — VRAM Consumer (显存占用)

`VRAM_CONSUMER` 插件：`enable()` 自动通过 `desk.acquire_vram` 申请显存，`disable()` 释放 / Auto VRAM acquire on enable, release on disable.

## 学到什么 / What you learn

- `VRAM_CONSUMER` 能力 + manifest `vram_mb=512`
- `lifecycle.enable("heavy_compute")` 自动申请 `vram_mb` 显存
- `lifecycle.disable("heavy_compute")` 自动释放
- `desk.vram_usage()` 查看当前各插件占用
- **必须 INLINE 模式**：PROCESS sandbox 申请 VRAM 会抛 `NotImplementedError`

## 代码要点 / Code highlights

`vram_plugin.py` manifest:

```python
HEAVY_COMPUTE_MANIFEST = PluginManifest(
    id="heavy_compute",
    name="Heavy Compute",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.VRAM_CONSUMER),
    entry_point=heavy_compute,
    timeout_seconds=120,
    vram_mb=512,
    sandbox_mode=SandboxMode.INLINE,
)
```

入口读 `vram_usage()` 并记日志:

```python
def heavy_compute(desk, params):
    size = int(params.get("matrix_size", 64))
    if desk is not None:
        usage = desk.vram_usage()
        desk.log("heavy_compute", "INFO", "computed", size=size, vram=usage)
    return {"matrix_size": size, "result_ok": True}
```

## 运行 / Run

```bash
pytest examples/ex06_vram/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_enable_acquires_vram` | enable 后 `vram_usage["heavy_compute"]==512` |
| `test_execute_returns_result` | execute 传 `matrix_size=128` 返回 `{"matrix_size":128,"result_ok":True}` |
| `test_disable_releases_vram` | disable 后 `heavy_compute` 从 `vram_usage()` 消失 |

## 关键点 / Key points

- 默认 `DeskRuntime()` 的 `vram_total_mb=0`（无上限）→ `acquire` 恒成功，便于单测
- VRAM 申请/释放由 lifecycle 管，插件自身**不**调 `acquire_vram`
- PROCESS sandbox 下 `VRAM_CONSUMER` 不可用 —— 用 INLINE
- 生产显存调度由 fusion-cowork 的 `DeskRuntime` 提供，本包不自行分配
