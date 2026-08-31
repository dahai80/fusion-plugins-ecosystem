"""Live MLX soak (步骤 5 长时稳定性，一次性脚本，不入 CI)。

对 live fusion-mlx 发起持续 tools/call，按间隔采样：
- 进程 RSS（检测内存泄漏）
- plugin_executions_total（单调、与实际调用数一致）
- plugin_errors_total（应为 0，无错误漂移）
- token_records（应 ≤ max_records，封顶不泄漏）
- active_sessions / log_entries（封顶不累积）

关键判定：持续运行后 RSS 不单调爬升、指标计数与调用数一致、
有界集合保持封顶。受脚本时长参数控制（默认 120s），24h 部署
由运维延长 --duration。

用法：
    python scripts/soak_mlx.py --duration 120 --interval 30

前置：fusion-mlx 已起 + Qwen3.8-27B-4bit 已加载。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

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
    desk.log("soak_mlx", "INFO", "mlx call", model=model)
    messages = [{"role": "user", "content": prompt}]
    resp = await desk.mlx_chat(model=model, messages=messages, max_tokens=max_tokens)
    text = ""
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices:
            text = choices[0].get("message", {}).get("content", "")
    return {"reply": text}


class _MLXHTTPClient:
    """最小 MLX 客户端：直连 fusion-mlx OpenAI 兼容端点，单连接复用。"""

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

    async def close(self):
        await self._client.aclose()


def rss_mb() -> float:
    """当前进程实时 RSS（MB），经 ps 读取（ru_maxrss 是高水位，不反映释放）。"""
    try:
        out = os.popen(f"ps -o rss= -p {os.getpid()}").read().strip()
        return float(out) / 1024.0 if out else 0.0
    except Exception:
        return 0.0


def build_runtime():
    desk = DeskRuntime()
    client = _MLXHTTPClient(BASE_URL, API_KEY)
    desk.mlx_client = client
    registry = PluginRegistry(desk=desk)
    registry.register(
        PluginManifest(
            id="soak_mlx",
            name="Soak MLX",
            version="0.1.0",
            category=PluginCategory.MLX_INFERENCE,
            description="live mlx soak",
            capabilities=[PluginCapability.MCP_TOOL],
            entry_point=mlx_entry,
            timeout_seconds=120,
        )
    )
    lifecycle = PluginLifecycle(registry)
    meter = TokenMeter(desk, max_records=500)
    lifecycle.load("soak_mlx")
    return desk, registry, lifecycle, meter, client


def sample(desk, meter, calls_done, rss_samples):
    exec_c = desk.metrics.get_counter("plugin_executions_total")
    err_c = desk.metrics.get_counter("plugin_errors_total")
    sess_g = desk.metrics.get_gauge("active_sessions")
    rss = rss_mb()
    rss_samples.append(rss)
    return {
        "calls_done": calls_done,
        "rss_mb": round(rss, 1),
        "exec_total": exec_c.total() if exec_c else 0,
        "err_total": err_c.total() if err_c else 0,
        "token_records": len(meter.all_records()),
        "active_sessions": sess_g.value() if sess_g else 0,
        "log_entries": len(desk.log_entries),
    }


async def run(duration, interval):
    desk, registry, lifecycle, meter, client = build_runtime()
    if not await client.health():
        print("[soak] fusion-mlx 不健康，退出")
        return None
    await lifecycle.enable("soak_mlx")

    print(
        f"[soak] model={MODEL} duration={duration}s interval={interval}s "
        f"max_records={meter._max_records}"
    )
    samples = []
    rss_samples = []
    calls_done = 0
    deadline = time.perf_counter() + duration
    t_last_sample = 0.0

    samples.append(sample(desk, meter, calls_done, rss_samples))
    print(f"[sample 0] {samples[-1]}")

    i = 0
    while time.perf_counter() < deadline:
        i += 1
        with meter.measure("soak_mlx", TokenKind.MLX_INFERENCE):
            result = await lifecycle.execute(
                "soak_mlx", {"prompt": "reply with exactly: ok", "max_tokens": 8}
            )
        if isinstance(result, dict) and result.get("reply"):
            calls_done += 1
        if time.perf_counter() - t_last_sample >= interval:
            t_last_sample = time.perf_counter()
            samples.append(sample(desk, meter, calls_done, rss_samples))
            print(f"[sample {len(samples) - 1}] {samples[-1]}")

    final = sample(desk, meter, calls_done, rss_samples)
    samples.append(final)
    print(f"[sample final] {final}")

    # 稳定性判定：后半段 RSS 是否持平（plateau）。
    # 首样本含模型预热 page-fault，不看首尾差；看后半段最大-最小 RSS。
    last = samples[-1]
    rss_series = [s["rss_mb"] for s in samples]
    half = (
        rss_series[len(rss_series) // 2 :] if len(rss_series) >= 4 else rss_series[-2:]
    )
    rss_plateau_delta = (max(half) - min(half)) if half else 0.0
    verdict = {
        "rss_first_mb": rss_series[0],
        "rss_last_mb": rss_series[-1],
        "rss_second_half_plateau_delta_mb": round(rss_plateau_delta, 1),
        "rss_climbing": rss_plateau_delta > 100,
        "exec_matches_calls": last["exec_total"] == calls_done,
        "err_drift": last["err_total"] != 0,
        "token_records_capped": last["token_records"] <= meter._max_records,
        "log_entries_capped": last["log_entries"] <= 2000,
        "calls_total": calls_done,
    }
    print("\n===== soak 判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    ok = (
        not verdict["rss_climbing"]
        and verdict["exec_matches_calls"]
        and not verdict["err_drift"]
        and verdict["token_records_capped"]
        and verdict["log_entries_capped"]
    )
    print(f"\n[verdict] {'STABLE' if ok else 'DRIFT DETECTED'}")
    print("======================")

    await lifecycle._sandbox.shutdown_all() if hasattr(
        lifecycle._sandbox, "shutdown_all"
    ) else None
    await client.close()
    return {"samples": samples, "verdict": verdict, "stable": ok}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=120, help="持续秒数")
    p.add_argument("--interval", type=int, default=30, help="采样间隔秒数")
    args = p.parse_args()
    res = asyncio.run(run(args.duration, args.interval))
    if res is not None:
        print("\n[result_json]\n" + json.dumps(res, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
