"""fusion-gateway shell 模块注册 — issue #30 验收补充 (对齐 fusion-cowork #97/#168)。

console 启动时向 fusion-gateway shell 注册表注册 web/manifest.json
(POST /api/v1/shell/manifests, admin-key auth) 并周期心跳保活
(心跳过期后网关将侧边栏入口置灰)。env 门控 — 需同时配置:
- FUSION_SHELL_GATEWAY_URL + FUSION_GATEWAY_ADMIN_KEY
可选 FUSION_SHELL_MANIFEST 覆盖 manifest 路径。未配置则静默跳过 (默认 OFF)。
/shell/fusion-plugins-ecosystem/ 动态代理由 manifest.upstream 驱动 (#232 契约)。

K4: 注册响应携带网关签发 fm- 模块密钥 (仅返回一次明文), 0600 落盘;
后续心跳用签发密钥鉴权, 失效 (401) 或网关重启 (404) 时用 admin key 重注册。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

GATEWAY_URL_ENV = "FUSION_SHELL_GATEWAY_URL"
ADMIN_KEY_ENV = "FUSION_GATEWAY_ADMIN_KEY"
MANIFEST_PATH_ENV = "FUSION_SHELL_MANIFEST"
UPSTREAM_OVERRIDE_ENV = "FUSION_PLUGINS_CONSOLE_UPSTREAM"
STATIC_KEY_ENV = "FUSION_PLUGINS_CONSOLE_API_KEY"
REGISTER_INTERVAL_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 5.0

# 网关签发 fm- 模块密钥落盘路径 (0600)。静态 env 密钥继续作为覆盖路径
# 向后兼容; 网关签发后文件覆盖 env 值 (双活窗口内两者皆可)。
MODULE_KEY_PATH = Path.home() / ".fusion-plugins-ecosystem" / "web_module_key"


def load_module_key() -> str:
    try:
        return MODULE_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_module_key(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    MODULE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODULE_KEY_PATH.write_text(key + "\n", encoding="utf-8")
    MODULE_KEY_PATH.chmod(0o600)
    logger.info("网关签发模块密钥已持久化: %s (%s)", MODULE_KEY_PATH, _mask_key(key))
    return True


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_issued_key(payload: Any) -> str:
    # 网关 (#261 K1) 签发响应的明文字段是 raw_key (writeShellKeyResponse);
    # 其余字段名保留兼容旧网关/自定义部署。
    if not isinstance(payload, dict):
        return ""
    for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("data")
    if isinstance(nested, dict):
        for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
            value = nested.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "web" / "manifest.json"


def shell_registration_enabled() -> bool:
    return bool(
        os.environ.get(GATEWAY_URL_ENV, "").strip()
        and os.environ.get(ADMIN_KEY_ENV, "").strip()
    )


def load_manifest() -> Dict[str, Any]:
    path = Path(
        os.environ.get(MANIFEST_PATH_ENV, "").strip() or _default_manifest_path()
    )
    if not path.exists():
        raise FileNotFoundError(f"shell manifest 不存在: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("id"):
        raise ValueError(f"shell manifest 缺少 id: {path}")
    override = os.environ.get(UPSTREAM_OVERRIDE_ENV, "").strip()
    if override:
        manifest["upstream"] = override
    if not manifest.get("upstream"):
        manifest["upstream"] = "http://127.0.0.1:8795"
    # 静态 api_key 仅在 env 配置时随注册体发送; 未配置时不发送 —
    # 网关 (K1) 签发 fm- 密钥并在响应中返回一次, 由 _registry_loop_body 持久化。
    api_key = os.environ.get(STATIC_KEY_ENV, "").strip()
    if api_key:
        manifest["api_key"] = api_key
    else:
        manifest.pop("api_key", None)
    logger.info(
        "shell manifest 已加载: %s (id=%s upstream=%s api_key=%s)",
        path,
        manifest["id"],
        manifest["upstream"],
        "set" if api_key else "unset",
    )
    return manifest


async def _registry_loop_body(
    client: Any, manifest: Dict[str, Any], interval: float
) -> None:
    module_id = str(manifest["id"])
    gateway = os.environ.get(GATEWAY_URL_ENV, "").strip().rstrip("/")
    admin_key = os.environ.get(ADMIN_KEY_ENV, "").strip()
    admin_headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    registered = False
    while True:
        try:
            if not registered:
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests",
                    json=manifest,
                    headers=admin_headers,
                )
                if resp.status_code == 200:
                    registered = True
                    logger.info("shell manifest 已注册: %s", module_id)
                    issued = _extract_issued_key(_safe_json(resp))
                    if issued and issued != load_module_key():
                        save_module_key(issued)
                    elif issued:
                        logger.info("网关签发密钥与已存值一致, 不重复落盘")
                else:
                    logger.warning(
                        "shell manifest 注册失败: %s %s",
                        resp.status_code,
                        resp.text[:200],
                    )
            else:
                module_key = load_module_key()
                beat_key = module_key or admin_key
                headers = {
                    "Authorization": f"Bearer {beat_key}",
                    "Content-Type": "application/json",
                }
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat",
                    headers=headers,
                )
                if resp.status_code == 404:
                    logger.info("shell 心跳 404, 重注册 %s", module_id)
                    registered = False
                elif resp.status_code == 401 and module_key:
                    logger.warning(
                        "shell 心跳 401 (签发密钥失效), 重注册 %s", module_id
                    )
                    registered = False
                elif resp.status_code != 200:
                    raise RuntimeError(
                        f"shell 心跳失败: {resp.status_code} {resp.text[:200]}"
                    )
        except asyncio.CancelledError:
            logger.info("shell 注册循环已取消 (module=%s)", module_id)
            raise
        except Exception as e:
            logger.warning("shell 注册循环错误 (将重试): %s", e)
        await asyncio.sleep(interval)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


async def shell_registry_loop(
    client: Any = None, interval: float = REGISTER_INTERVAL_SECONDS
) -> None:
    """注册一次, 心跳保活; 网关重启 (404) 后重注册。失败仅告警重试, 不影响服务。"""
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error("shell 注册禁用 (manifest 不可用): %s", e)
        return
    if client is None:
        import httpx

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as own:
            await _registry_loop_body(own, manifest, interval)
    else:
        await _registry_loop_body(client, manifest, interval)
