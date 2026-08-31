"""Live MLX throughput bench (步骤 3 压测，一次性脚本，不入 CI)。

对 live fusion-mlx 发起 N 路并发 tools/call（经 lifecycle.execute →
desk.mlx_chat），测吞吐/延迟，并验证指标计数器在并发下一致。

用法：
    python scripts/bench_mlx_load.py --concurrency 8 --total 32

前置：fusion-mlx 已起 + Qwen3.8-27B-4bit 已加载。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

# 确保用仓库内包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)
from fusion_plugins_ecosystem.token_meter import TokenKind, TokenMeter

API_KEY = "dahai168"
BASE_URL = "http://127.0.0.1:11434"
MODEL = "Qwen3.8-27B-4bit"


async def mlx_entry(desk, params):
    model = params.get("model", MODEL)
    prompt = params.get("prompt", "reply with exactly: ok")
    max_tokens = int(params.get("max_tokens", 8))
    if desk is None:
        return {"error": "desk unavailable"}
    desk.log("bench_mlx", "INFO", "mlx call", model=model, chars=len(prompt))
    messages = [{"role": "user", "content": prompt}]
    resp = await desk.mlx_chat(model=model, messages=messages, max_tokens=max_tokens)
    text = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            text = choices[0].get("message", {}).get("content", "")
    return {"reply": text}


class _MLXHTTPClient:
    """最小 MLX 客户端：直接走 fusion-mlx OpenAI 兼容端点。

    满足 desk.mlx_chat 的契约（async .chat(model, messages, **kwargs)），
    不依赖 fusion_core/fusion_cowork 导入路径，压测自包含。
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300,
        )

    async def chat(self, model, messages, **kwargs):
        payload = {"model": model, "messages": messages}
        payload.update(kwargs)
        r = await self._client.post("/v1/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()

    async def health(self):
        try:
            r = await self._client.get("/health")
            return r.status_code == 200
        except Exception:
            return False


def build_runtime():
    desk = DeskRuntime()
    # 注入真 MLX 客户端（直连 fusion-mlx OpenAI 兼容端点）
    desk.mlx_client = _MLXHTTPClient(BASE_URL, API_KEY)
    registry = PluginRegistry(desk=desk)
    registry.register(
        PluginManifest(
            id="bench_mlx",
            name="Bench MLX",
            version="0.1.0",
            category=PluginCategory.MLX_INFERENCE,
            description="live mlx throughput bench",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=mlx_entry,
            timeout_seconds=120,
        )
    )
    lifecycle = PluginLifecycle(registry)
    meter = TokenMeter(desk)
    lifecycle.load("bench_mlx")
    return desk, registry, lifecycle, meter


async def one_call(lifecycle, meter, i):
    start = time.perf_counter()
    with meter.measure("bench_mlx", TokenKind.MLX_INFERENCE):
        result = await lifecycle.execute(
            "bench_mlx", {"prompt": "reply with exactly: ok", "max_tokens": 8}
        )
    elapsed = time.perf_counter() - start
    return elapsed, result


async def run(concurrency, total):
    desk, registry, lifecycle, meter = build_runtime()
    await lifecycle.enable("bench_mlx")

    print(f"[bench] model={MODEL} concurrency={concurrency} total={total}")
    sem = asyncio.Semaphore(concurrency)

    async def bounded(i):
        async with sem:
            return await one_call(lifecycle, meter, i)

    t0 = time.perf_counter()
    results = await asyncio.gather(*(bounded(i) for i in range(total)))
    wall = time.perf_counter() - t0

    latencies = [r[0] for r in results]
    ok = sum(1 for r in results if isinstance(r[1], dict) and r[1].get("reply"))
    fail = total - ok

    exec_counter = desk.metrics.get_counter("plugin_executions_total")
    err_counter = desk.metrics.get_counter("plugin_errors_total")
    err_total = err_counter.total() if err_counter else 0

    print("\n===== MLX 压测结果 =====")
    print(f"wall_seconds       : {wall:.2f}")
    print(f"throughput(req/s)  : {total / wall:.2f}")
    print(f"success / fail     : {ok} / {fail}")
    if latencies:
        lat_sorted = sorted(latencies)
        print(
            f"latency p50 / p90 / p99 / max: "
            f"{statistics.median(latencies):.2f} / "
            f"{lat_sorted[int(len(lat_sorted) * 0.9)]:.2f} / "
            f"{lat_sorted[int(len(lat_sorted) * 0.99)]:.2f} / "
            f"{max(latencies):.2f}s"
        )
    print(f"plugin_executions_total: {exec_counter.total()}")
    print(f"plugin_errors_total    : {err_total}")
    print(f"token records          : {len(meter.all_records())}")
    print("========================")

    await lifecycle._sandbox.shutdown_all() if hasattr(
        lifecycle._sandbox, "shutdown_all"
    ) else None
    return {
        "model": MODEL,
        "concurrency": concurrency,
        "total": total,
        "wall_seconds": round(wall, 2),
        "throughput": round(total / wall, 2),
        "success": ok,
        "fail": fail,
        "median_latency": round(statistics.median(latencies), 3) if latencies else None,
        "max_latency": round(max(latencies), 3) if latencies else None,
        "exec_total": exec_counter.total(),
        "err_total": err_total,
        "token_records": len(meter.all_records()),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--total", type=int, default=32)
    args = p.parse_args()
    res = asyncio.run(run(args.concurrency, args.total))
    # 输出 JSON 供审计文档引用
    import json

    print("\n[result_json]\n" + json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
