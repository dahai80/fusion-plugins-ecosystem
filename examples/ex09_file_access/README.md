# ex09 — File Access (文件权限)

`FILE_ACCESS` 能力。读文件前先查 `desk.check_file_permission(plugin_id, path)` / Permission gate before file read.

## 学到什么 / What you learn

- `FILE_ACCESS` 能力 + `desk.check_file_permission(plugin_id, path)` 前置校验
- 空 allowlist = 允许全部（单测便利）
- `desk.grant_permission(plugin_id, [dirs])` 限定到指定目录，**前缀匹配**
- 授权外路径 → 插件返回 `{"error":"permission denied"}`，不抛异常
- 测试用 `tempfile.TemporaryDirectory()`，teardown 自动清理过程数据

## 代码要点 / Code highlights

`file_plugin.py` 入口先查权限再读:

```python
def read_file_plugin(desk, params):
    path = params.get("path", "")
    if desk is None:
        return {"error": "desk unavailable"}
    allowed = desk.check_file_permission("read_file_plugin", path)
    if not allowed:
        desk.log("read_file_plugin", "WARN", "permission denied", path=path)
        return {"error": "permission denied", "path": path}
    if not os.path.isfile(path):
        desk.log("read_file_plugin", "WARN", "not a file", path=path)
        return {"error": "not a file", "path": path}
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    desk.log("read_file_plugin", "INFO", "read", path=path, chars=len(content))
    return {"path": path, "content": content, "chars": len(content)}
```

manifest 声明文件访问能力:

```python
READ_FILE_MANIFEST = PluginManifest(
    id="read_file_plugin",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.FILE_ACCESS),
    entry_point=read_file_plugin,
    timeout_seconds=30,
)
```

## 运行 / Run

```bash
pytest examples/ex09_file_access/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_allowed_file_reads` | grant 临时目录后，目录内文件读出 `content=="hello world"`、`chars==11` |
| `test_outside_file_denied` | grant `inside`，读 `outside` 文件 → `{"error":"permission denied"}` |
| `test_empty_allowlist_allows_all` | 不调 `grant_permission` → 空 allowlist → 任意路径可读 |

## 关键点 / Key points

- 权限校验在**插件入口内**主动调 `check_file_permission`，非 lifecycle 自动拦截
- `grant_permission` 是前缀匹配：目录前缀覆盖其下所有文件
- 权限拒绝是软返回（error dict），便于上游 MCP `isError` 判断
- 临时文件用 `TemporaryDirectory` 上下文管理器，退出即清理，只留最终日志
