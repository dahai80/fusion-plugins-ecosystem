# Examples — fusion-plugins-ecosystem

Runnable plugin samples, organized by scenario. Each directory is self-contained: a manifest module, a test, and a README.

> Companion to [../docs/PLUGIN_DEVELOPMENT.md](../docs/PLUGIN_DEVELOPMENT.md) (EN) / [../docs/PLUGIN_DEVELOPMENT_CN.md](../docs/PLUGIN_DEVELOPMENT_CN.md) (中文).

## Run

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
cd fusion-plugins-ecosystem
pytest examples/ -v
```

Examples use the package's public API (`PluginRegistry`, `PluginLifecycle`, `ClaudeGateway`, `SkillAdapter`). `asyncio_mode=auto` is on — no `@pytest.mark.asyncio` needed.

## Scenarios

| # | Dir | Scenario | Teaches |
|---|---|---|---|
| 01 | [`ex01_echo/`](ex01_echo/) | minimal sync plugin | `MCP_TOOL` + `CLAUDE_SKILL`, manifest, register→enable→execute |
| 02 | [`ex02_async/`](ex02_async/) | async entry point | `async def` auto-detection via `inspect.iscoroutinefunction` |
| 03 | [`ex03_skill_export/`](ex03_skill_export/) | Skill bundle | `SkillAdapter.export_skill()` → SKILL.md frontmatter + body |
| 04 | [`ex04_subagent/`](ex04_subagent/) | reverse subagent | `SUBAGENT` capability + `ClaudeGateway.dispatch_subagent` |
| 05 | [`ex05_mlx_chat/`](ex05_mlx_chat/) | local MLX inference | `MLX_INFERENCE` + `desk.mlx_chat()` (needs fusion-mlx running) |
| 06 | [`ex06_vram/`](ex06_vram/) | VRAM consumer | `VRAM_CONSUMER` + `vram_mb`, INLINE only |
| 07 | [`ex07_long_task/`](ex07_long_task/) | long task + restart | `LONG_TASK` + timeout meltdown + auto-restart |
| 08 | [`ex08_process_sandbox/`](ex08_process_sandbox/) | process isolation | `SandboxMode.PROCESS`, `module:attr` entry point |
| 09 | [`ex09_file_access/`](ex09_file_access/) | file permission | `FILE_ACCESS` + `desk.check_file_permission` |

## Conventions used

- Entry points are **module-level functions** (so PROCESS-mode 08 can import them too — same pattern everywhere).
- Logs via `desk.log(...)`, never `print()`.
- Tests clean up temp data; only final outputs + logs kept.
- 4-space indent, no docstrings (matches project style).
