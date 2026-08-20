# ex03 — Skill Export (Claude Skill 导出)

插件导出为 Claude Code Skill bundle / Export plugin as a Claude Code Skill.

## 学到什么 / What you learn

- `CLAUDE_SKILL` 能力 → `SkillAdapter.export_skill(id)` 生成 SkillBundle
- SkillBundle = `skill_md`（frontmatter + body）+ `references`（辅助文档）+ `scripts`
- frontmatter 自动生成 `name`/`version`/`capabilities`
- body 含 `## Parameters` 与 `## Input Schema`（JSON Schema 代码块）
- `MCPAnnotations` 控制工具行为提示（readOnly/idempotent/openWorld）

## 代码要点 / Code highlights

`skill_plugin.py` 关键 manifest 字段:

```python
FORMAT_REPORT_MANIFEST = PluginManifest(
    id="format_report",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.CLAUDE_SKILL),
    params=(
        PluginParam(name="title", type=PluginParamType.STRING, required=True, ...),
        PluginParam(name="sections", type=PluginParamType.ARRAY, ...),
    ),
    mcp_annotations=MCPAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False
    ),
    output_schema={"type": "string", "format": "markdown"},
    ...
)
```

导出调用:

```python
bundle = SkillAdapter(registry).export_skill("format_report")
bundle.skill_md  # 完整 Skill markdown
bundle.references  # {"overview.md": "..."} 辅助文档
```

## 运行 / Run

```bash
pytest examples/ex03_skill_export/ -v
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_skill_bundle_frontmatter` | `name: format_report`、`version: 0.1.0`、`capabilities: [mcp_tool, claude_skill]` |
| `test_skill_bundle_body_params_and_schema` | body 含 `## Parameters`、`**title**`、`## Input Schema`；JSON Schema `required` 含 `title` |
| `test_references_generated` | `references` 含 `overview.md`，内容含 "Format Report" |

## 关键点 / Key points

- `capabilities` 在 frontmatter 中用逗号连接值（`[mcp_tool, claude_skill]`）
- Input Schema 由 `PluginParam` 列表自动生成 JSON Schema
- 仅声明 `CLAUDE_SKILL` 能力的插件才会被 `SkillAdapter` 导出
