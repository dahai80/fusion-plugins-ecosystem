# fusion-gateway shell module registration — issue #30 acceptance follow-up
# (aligned with fusion-cowork #97/#168).
# On startup the console registers web/manifest.json with the fusion-gateway
# shell registry (POST /api/v1/shell/manifests, admin-key auth) and keeps it
# alive via periodic heartbeats (when the heartbeat expires the gateway
# grays out the sidebar entry). Env-gated — enabled when FUSION_ADMIN_KEY is
# configured (config-conventions §1: the admin key doubles as the shell
# registration credential).
# Gateway address via FUSION_GATEWAY_BASE_URL (fusion-core single-point
# resolution, dev default localhost 11432). Optional FUSION_SHELL_MANIFEST
# overrides the manifest path. When unconfigured this silently skips
# (default OFF). The /shell/fusion-plugins-ecosystem/ dynamic proxy is
# driven by manifest.upstream (#232 contract).
# K4: the registration response carries the gateway-issued fm- module key
# (plaintext returned once only), persisted 0600; subsequent heartbeats
# authenticate with the issued key, and on expiry (401) or gateway restart
# (404) re-register with the admin key.
# issue #38: key/URL resolution moved to fusion_core.config (admin_key /
# gateway_base_url, prod fail-fast on missing key, dev warns and continues)
# — this module no longer hand-rolls env parsing.

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fusion_core.config import admin_key, gateway_base_url

from fusion_plugins_ecosystem.config import DATA_DIR

logger = logging.getLogger(__name__)

MANIFEST_PATH_ENV = "FUSION_SHELL_MANIFEST"
UPSTREAM_OVERRIDE_ENV = "FUSION_PLUGINS_CONSOLE_UPSTREAM"
STATIC_KEY_ENV = "FUSION_PLUGINS_CONSOLE_API_KEY"
REGISTER_INTERVAL_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 5.0

# Persisted path (0600) for the gateway-issued fm- module key. The static
# env key remains an override path for backward compatibility; once the
# gateway issues a key the file overrides the env value (during the
# dual-active window both are accepted).
MODULE_KEY_PATH = DATA_DIR / "web_module_key"


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
    logger.info("gateway-issued module key persisted: %s (%s)", MODULE_KEY_PATH, _mask_key(key))
    return True


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_issued_key(payload: Any) -> str:
    # The gateway (#261 K1) issued-key response plaintext field is raw_key
    # (writeShellKeyResponse); the other field names stay for compatibility
    # with older gateways / custom deployments.
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
    # issue #38: gate = whether the admin key resolves (dev missing → empty
    # string + warning → OFF; prod missing → RuntimeError, fail-fast on the
    # startup path).
    return bool(admin_key())


def load_manifest() -> dict[str, Any]:
    path = Path(
        os.environ.get(MANIFEST_PATH_ENV, "").strip() or _default_manifest_path()
    )
    if not path.exists():
        raise FileNotFoundError(f"shell manifest does not exist: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("id"):
        raise ValueError(f"shell manifest missing id: {path}")
    override = os.environ.get(UPSTREAM_OVERRIDE_ENV, "").strip()
    if override:
        manifest["upstream"] = override
    if not manifest.get("upstream"):
        manifest["upstream"] = "http://127.0.0.1:8795"
    # The static api_key is only sent in the registration body when
    # configured via env; not sent otherwise — the gateway (K1) issues the
    # fm- key and returns it once in the response, persisted by
    # _registry_loop_body.
    api_key = os.environ.get(STATIC_KEY_ENV, "").strip()
    if api_key:
        manifest["api_key"] = api_key
    else:
        manifest.pop("api_key", None)
    logger.info(
        "shell manifest loaded: %s (id=%s upstream=%s api_key=%s)",
        path,
        manifest["id"],
        manifest["upstream"],
        "set" if api_key else "unset",
    )
    return manifest


async def _registry_loop_body(
    client: Any, manifest: dict[str, Any], interval: float
) -> None:
    module_id = str(manifest["id"])
    gateway = gateway_base_url().rstrip("/")
    adm = admin_key()
    admin_headers = {
        "Authorization": f"Bearer {adm}",
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
                    logger.info("shell manifest registered: %s", module_id)
                    issued = _extract_issued_key(_safe_json(resp))
                    if issued and issued != load_module_key():
                        save_module_key(issued)
                    elif issued:
                        logger.info("gateway-issued key matches stored value, skipping re-persist")
                else:
                    logger.warning(
                        "shell manifest registration failed: %s %s",
                        resp.status_code,
                        resp.text[:200],
                    )
            else:
                module_key = load_module_key()
                beat_key = module_key or adm
                headers = {
                    "Authorization": f"Bearer {beat_key}",
                    "Content-Type": "application/json",
                }
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat",
                    headers=headers,
                )
                if resp.status_code == 404:
                    logger.info("shell heartbeat 404, re-registering %s", module_id)
                    registered = False
                elif resp.status_code == 401 and module_key:
                    logger.warning(
                        "shell heartbeat 401 (issued key invalid), re-registering %s", module_id
                    )
                    registered = False
                elif resp.status_code != 200:
                    raise RuntimeError(
                        f"shell heartbeat failed: {resp.status_code} {resp.text[:200]}"
                    )
        except asyncio.CancelledError:
            logger.info("shell registration loop cancelled (module=%s)", module_id)
            raise
        except Exception as e:
            logger.warning("shell registration loop error (will retry): %s", e)
        await asyncio.sleep(interval)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


async def shell_registry_loop(
    client: Any = None, interval: float = REGISTER_INTERVAL_SECONDS
) -> None:
    # Register once, keep alive via heartbeats; re-register after a gateway
    # restart (404). Failures only warn and retry, never affecting the service.
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error("shell registration disabled (manifest unavailable): %s", e)
        return
    if client is None:
        import httpx

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as own:
            await _registry_loop_body(own, manifest, interval)
    else:
        await _registry_loop_body(client, manifest, interval)
