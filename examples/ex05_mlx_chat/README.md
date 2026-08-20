# ex05 — MLX Chat (本地推理)

通过 `desk.mlx_chat()` 调用本地 fusion-mlx 引擎 / Call local fusion-mlx engine.

## 学到什么 / What you learn

- `MLX_INFERENCE` 分类 + `desk.mlx_chat(model, messages, max_tokens)`
- MLX 是**远程推理**：本插件不加载模型，只通过 HTTP 调 `localhost:11434`
- 测试用 fake async 函数 stub `desk.mlx_chat`，无需真引擎
- 真实运行需先启动 fusion-mlx

## 代码要点 / Code highlights

`mlx_plugin.py` 入口:

```python
async def mlx_chat_plugin(desk, params):
    model = params.get("model", "Qwen2.5-0.5B-Instruct")
    prompt = params.get("prompt", "")
    max_tokens = int(params.get("max_tokens", 128))
    if desk is None:
        return {"error": "desk unavailable"}
    messages = [{"role": "user", "content": prompt}]
    resp = await desk.mlx_chat(model=model, messages=messages, max_tokens=max_tokens)
    text = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            text = choices[0].get("message", {}).get("content", "")
        else:
            text = resp.get("content", "")
    elif isinstance(resp, str):
        text = resp
    return {"model": model, "reply": text}
```

## 运行 / Run

测试（stub，无需引擎）:

```bash
pytest examples/ex05_mlx_chat/ -v
```

真实推理（需先起引擎）:

```bash
~/claude-home/fusion-mlx/start.sh start
# 然后用真实 DeskRuntime（mlx_client 注入）执行插件
```

## 测试覆盖 / Test coverage

| 测试 | 断言 |
|------|------|
| `test_mlx_chat_parses_choices_content` | dict 响应 `choices[0].message.content` → `reply` |
| `test_mlx_chat_string_response` | 纯字符串响应 → `reply` |

## 关键点 / Key points

- `desk.mlx_chat` 是 async，入口须 `async def`
- 响应兼容 OpenAI 格式（`choices[].message.content`）与降级（`content` 字段 / 纯字符串）
- **不要在本插件 import fusion-mlx**；统一走 `DeskRuntime`
- 默认 `DeskRuntime()`（`mlx_client=None`）下 `mlx_chat` 降级，测试须 stub
