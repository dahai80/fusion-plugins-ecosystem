"""Example 05 - tests for MLX chat plugin.

Stubs desk.mlx_chat so tests run without the fusion-mlx engine.
For a LIVE run, start the engine first:
    ~/claude-home/fusion-mlx/start.sh start
"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

from examples.ex05_mlx_chat.mlx_plugin import MLX_CHAT_MANIFEST


def _make_desk(reply_text: str) -> DeskRuntime:
    desk = DeskRuntime()

    async def fake_mlx_chat(model, messages, **kwargs):
        return {
            "choices": [{"message": {"content": reply_text}}],
            "model": model,
        }

    desk.mlx_chat = fake_mlx_chat  # type: ignore[assignment]
    return desk


async def test_mlx_chat_parses_choices_content():
    desk = _make_desk("hello from mlx")
    registry = fpe.PluginRegistry(desk=desk)
    registry.register(MLX_CHAT_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)

    await lifecycle.enable("mlx_chat_plugin")
    result = await lifecycle.execute(
        "mlx_chat_plugin", {"prompt": "say hi", "max_tokens": 8}
    )

    assert result["model"] == "Qwen2.5-0.5B-Instruct"
    assert result["reply"] == "hello from mlx"

    await lifecycle.disable("mlx_chat_plugin")
    lifecycle.unload("mlx_chat_plugin")


async def test_mlx_chat_string_response():
    desk = DeskRuntime()

    async def fake_str(model, messages, **kwargs):
        return "plain string reply"

    desk.mlx_chat = fake_str  # type: ignore[assignment]
    registry = fpe.PluginRegistry(desk=desk)
    registry.register(MLX_CHAT_MANIFEST)
    lifecycle = fpe.PluginLifecycle(registry)

    await lifecycle.enable("mlx_chat_plugin")
    result = await lifecycle.execute("mlx_chat_plugin", {"prompt": "x"})
    assert result["reply"] == "plain string reply"

    await lifecycle.disable("mlx_chat_plugin")
    lifecycle.unload("mlx_chat_plugin")
