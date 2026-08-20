"""Example 05 - local MLX inference via desk.mlx_chat().

Category MLX_INFERENCE. Calls the local fusion-mlx engine through DeskRuntime.
Requires fusion-mlx running on localhost:11434 for live calls; tests stub
desk.mlx_chat so they run without the engine.
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


async def mlx_chat_plugin(desk, params):
    model = params.get("model", "Qwen2.5-0.5B-Instruct")
    prompt = params.get("prompt", "")
    max_tokens = int(params.get("max_tokens", 128))
    if desk is None:
        return {"error": "desk unavailable"}
    desk.log("mlx_chat_plugin", "INFO", "calling mlx", model=model, chars=len(prompt))
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


MLX_CHAT_MANIFEST = PluginManifest(
    id="mlx_chat_plugin",
    name="MLX Chat",
    version="0.1.0",
    category=PluginCategory.MLX_INFERENCE,
    description="Local MLX inference via desk.mlx_chat().",
    capabilities=(PluginCapability.MCP_TOOL,),
    params=(
        PluginParam(
            name="model",
            type=PluginParamType.STRING,
            description="MLX model id.",
            required=False,
            default="Qwen2.5-0.5B-Instruct",
        ),
        PluginParam(
            name="prompt",
            type=PluginParamType.STRING,
            description="User prompt.",
            required=True,
        ),
        PluginParam(
            name="max_tokens",
            type=PluginParamType.INT,
            description="Max tokens to generate.",
            required=False,
            default=128,
        ),
    ),
    entry_point=mlx_chat_plugin,
    timeout_seconds=300,
)
