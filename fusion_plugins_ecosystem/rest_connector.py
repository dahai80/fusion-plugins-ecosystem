"""声明式 REST/Webhook 连接器包（issue #30 E3，通用化自 f-ecommerce connectors.py）。

一个连接器 = 一份 JSON 文档（config-first 接入，无需适配器类）：

{
  "id": "erp-json",
  "kind": "rest",                  // rest | webhook | db
  "auth": {"type": "bearer", "credential_fields": ["token"]},
  "base_url": "https://erp.example.com/api",
  "credential_ref": "erp-json",    // Desk 加密凭据存储键（Fernet）
  "operations": {
    "erp.stock.fetch": {"method": "GET", "path": "/stock",
                        "params": {"since": "{since}"},
                        "response": {"rows": "$.rows"}}
  },
  "error_code_map": {"40001": "auth failed"}
}

凭据永不写入连接器 JSON —— enable 时经 PUT /api/adapters/<id> 存入
Desk 加密存储（Fernet），调用时服务端解析。执行结果契约（全连接器统一）：
  {"ok": bool, "status": int|None, "data": ..., "extracted": ...,
   "error_code": ..., "reason": ..., "duration_ms": int}

读通道（GET）enable 时自动桥接为能力；写通道（POST/PUT/PATCH/DELETE）
不自动列出，进入人工审核队列（§6.2 契约）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fusion_plugins_ecosystem.config import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_CONNECTOR_DIR = str(DATA_DIR / "connectors")  # issue #38：数据目录单点解析

_READ_METHODS = ("GET",)
_VALID_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_VALID_KINDS = ("rest", "webhook", "db")

# JSONPath-lite: "$.a.b.0" -> data["a"]["b"][0]
_PATH_RE = re.compile(r"^\$([.\w\[\]]*)$")


def connector_dir() -> Path:
    return Path(
        os.environ.get("FUSION_PLUGIN_CONNECTOR_DIR", DEFAULT_CONNECTOR_DIR)
    ).expanduser()


def _doc_path(connector_id: str) -> Path:
    return connector_dir() / f"{connector_id}.json"


def get_connector(connector_id: str) -> dict[str, Any] | None:
    path = _doc_path(connector_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.error("rest_connector: 连接器 %s 文件不可读", connector_id)
        return None


def save_connector(connector_id: str, document: dict[str, Any]) -> None:
    d = connector_dir()
    d.mkdir(parents=True, exist_ok=True)
    _doc_path(connector_id).write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("rest_connector: 连接器 %s 已保存到 %s", connector_id, d)


def delete_connector(connector_id: str) -> bool:
    path = _doc_path(connector_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info("rest_connector: 连接器 %s 已删除", connector_id)
    return True


def list_connectors() -> list[dict[str, Any]]:
    out = []
    d = connector_dir()
    if d.exists():
        for path in sorted(d.glob("*.json")):
            doc = get_connector(path.stem)
            if doc:
                out.append(
                    {
                        "id": doc.get("id", path.stem),
                        "kind": doc.get("kind"),
                        "operations": list(doc.get("operations", {})),
                    }
                )
    return out


def connector_report() -> list[dict[str, Any]]:
    return [
        {**entry, "problems": validate_connector(get_connector(entry["id"]) or {})}
        for entry in list_connectors()
    ]


def validate_connector(doc: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not doc.get("id"):
        problems.append("missing id")
    if doc.get("kind") not in _VALID_KINDS:
        problems.append("kind must be rest|webhook|db")
    if doc.get("kind") == "rest" and not doc.get("base_url"):
        problems.append("rest connector requires base_url")
    operations = doc.get("operations")
    if not isinstance(operations, dict) or not operations:
        problems.append("operations must be a non-empty object")
        return problems
    for name, op in operations.items():
        if not isinstance(op, dict) or not op.get("path"):
            problems.append(f"operation '{name}' missing path")
        method = str(op.get("method", "GET")).upper()
        if method not in _VALID_METHODS:
            problems.append(f"operation '{name}' invalid method {method}")
        for key in ("response", "params", "headers"):
            spec = op.get(key, {})
            if not isinstance(spec, dict):
                problems.append(f"operation '{name}' {key} must be an object")
        for dotted in (op.get("response") or {}).values():
            if not isinstance(dotted, str) or not _PATH_RE.match(dotted):
                problems.append(
                    f"operation '{name}' response path {dotted!r} "
                    "must be JSONPath-lite ($.a.b)"
                )
    auth = doc.get("auth", {})
    if auth and not auth.get("credential_fields"):
        problems.append("auth requires credential_fields")
    return problems


def is_read_op(op: dict[str, Any]) -> bool:
    return str(op.get("method", "GET")).upper() in _READ_METHODS


def read_channels(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """READ 通道 —— enable 时自动列为能力的集合（§6.2）。"""
    channels = []
    for op_name, op in (doc.get("operations") or {}).items():
        if is_read_op(op):
            channels.append(
                {
                    "id": op_name,
                    "title": op_name,
                    "description": f"{op_name} via connector {doc.get('id', '?')}",
                }
            )
    return channels


def write_channels(doc: dict[str, Any]) -> list[str]:
    return [
        op_name
        for op_name, op in (doc.get("operations") or {}).items()
        if not is_read_op(op)
    ]


def _extract(data: Any, dotted: str) -> Any:
    """'$.product_id' / '$.data.list.0.id' -> 值；缺失返回 None。"""
    if not dotted.startswith("$"):
        return None
    value = data
    for part in dotted[2:].split("."):
        if part == "":
            continue
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _render(text: str, variables: dict[str, Any]) -> str:
    for key, value in (variables or {}).items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _apply_auth(
    headers: dict[str, str],
    doc: dict[str, Any],
    credentials: dict[str, Any],
) -> str | None:
    """按 auth 声明注入凭据头；缺字段返回缺失原因（fail-loud 契约）。"""
    auth = doc.get("auth") or {}
    auth_type = str(auth.get("type", ""))
    cred_fields = auth.get("credential_fields") or []
    missing = [f for f in cred_fields if not credentials.get(f)]
    if missing:
        return f"missing credential fields: {missing}"
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {credentials[cred_fields[0]]}"
    elif auth_type == "api_key_header":
        headers[str(auth.get("header", "X-API-Key"))] = str(credentials[cred_fields[0]])
    return None


async def execute_operation(
    connector: dict[str, Any],
    operation_name: str,
    *,
    credentials: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行一个连接器操作；永不抛异常 —— 统一返回契约 envelope。"""
    import httpx

    op = (connector.get("operations") or {}).get(operation_name)
    if op is None:
        return {
            "ok": False,
            "error_code": "unknown_operation",
            "reason": f"operation '{operation_name}' not defined",
        }
    kind = connector.get("kind", "rest")
    if kind == "db":
        return {
            "ok": False,
            "error_code": "not_implemented",
            "reason": "db connector ships in a later phase (read-only)",
        }
    if kind not in ("rest", "webhook"):
        return {
            "ok": False,
            "error_code": "unknown_kind",
            "reason": f"kind '{kind}' unsupported",
        }

    started = time.monotonic()
    try:
        if kind == "rest":
            result = await _exec_rest(
                connector, op, credentials or {}, variables or {}, payload or {}
            )
        else:
            result = await _exec_webhook(connector, op, payload or {})
    except httpx.TimeoutException:
        result = {"ok": False, "error_code": "timeout", "reason": "request timed out"}
    except httpx.HTTPError as exc:
        result = {"ok": False, "error_code": "http_error", "reason": str(exc)[:200]}
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    logger.info(
        "rest_connector:%s %s -> %s (%sms)",
        connector.get("id"),
        operation_name,
        "ok" if result.get("ok") else result.get("error_code"),
        result.get("duration_ms", 0),
    )
    return result


async def _exec_rest(
    connector: dict[str, Any],
    op: dict[str, Any],
    credentials: dict[str, Any],
    variables: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    import httpx

    method = str(op.get("method", "GET")).upper()
    url = _render(
        str(connector.get("base_url", "")).rstrip("/") + str(op.get("path", "")),
        variables,
    )
    headers = {"Content-Type": "application/json"}
    auth_error = _apply_auth(headers, connector, credentials)
    if auth_error:
        return {"ok": False, "error_code": "missing_credentials", "reason": auth_error}

    params = {
        k: _render(str(v), variables) for k, v in (op.get("params") or {}).items()
    }
    body = None if method == "GET" else (payload or {})
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, url, headers=headers, params=params, json=body
        )
    if resp.status_code >= 400:
        error_code = f"http_{resp.status_code}"
        reason = resp.text[:200]
        for code, message in (connector.get("error_code_map") or {}).items():
            if code in resp.text:
                error_code, reason = code, message
                break
        return {
            "ok": False,
            "status": resp.status_code,
            "error_code": error_code,
            "reason": reason,
        }
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {"raw": resp.text[:500]}
    extracted = {}
    for key, dotted in (op.get("response") or {}).items():
        value = _extract(data, dotted)
        if value is not None:
            extracted[key] = value
    return {
        "ok": True,
        "status": resp.status_code,
        "data": data,
        "extracted": extracted,
    }


async def _exec_webhook(
    connector: dict[str, Any],
    op: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """出站 webhook 投递：POST payload 到声明路径（通用化：不做电商路由）。"""
    import httpx

    url = _render(
        str(connector.get("base_url", "")).rstrip("/") + str(op.get("path", "")),
        payload or {},
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload or {})
    return {
        "ok": resp.status_code < 400,
        "status": resp.status_code,
        "data": {"delivered": resp.status_code < 400},
    }
