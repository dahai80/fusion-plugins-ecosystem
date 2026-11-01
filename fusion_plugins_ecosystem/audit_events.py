"""能力操作审计事件流（issue #43 PC-3）。

hub 的 install / uninstall / OAuth 授权 / scope 策略变更事件写入
fusion-security 审计管道的 JSONL 落点（`~/.fusion_security/audit/audit_<tenant>.jsonl`，
与 fusion_security.engine.tenant.audit.AuditLogger 同格式 — security#61
审计工作台直接可检索），trace_id 串跨服务链路。

只依赖文件契约，不 import fusion_security（跨工程零耦合）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "system"

_TENANT_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def new_trace_id() -> str:
    """跨服务链路 trace_id：fp-<uuid4 前 16 hex>。"""
    return "fp-" + uuid.uuid4().hex[:16]


def _safe_tenant_slug(tenant_id: str) -> str:
    slug = _TENANT_SAFE.sub("_", tenant_id or "").strip("._-")
    return slug or "system"


def audit_log_dir() -> Path:
    # 惰性读 env — import 时取值会让测试 monkeypatch 失效（事件漏进真实目录）
    return Path(
        os.environ.get(
            "FUSION_PLUGIN_AUDIT_DIR",
            str(Path.home() / ".fusion_security" / "audit"),
        )
    )


def _sink_enabled() -> bool:
    # 关闭落盘（测试/离线场景）：FUSION_PLUGIN_AUDIT_SINK=off
    return os.environ.get("FUSION_PLUGIN_AUDIT_SINK", "on").lower() != "off"


def _default_tenant() -> str:
    return os.environ.get("FUSION_PLUGIN_AUDIT_TENANT", DEFAULT_TENANT)


def emit(
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    details: dict[str, Any] | None = None,
    risk_level: str = "info",
    trace_id: str = "",
    actor: str = "hub-console",
    tenant_id: str = "",
) -> dict[str, Any]:
    """追加一条审计事件（AuditEntry 同构 JSONL 行）。

    失败不拖垮业务流 — 审计落盘降级为告警日志。
    """
    entry = {
        "action": action,
        "actor": actor,
        "tenant_id": tenant_id or _default_tenant(),
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details or {},
        "timestamp": time.time(),
        "ip_address": "",
        "risk_level": risk_level,
        "trace_id": trace_id or new_trace_id(),
    }
    if not _sink_enabled():
        logger.debug("audit_events: sink off, drop %s %s", action, resource_id)
        return entry
    try:
        path = audit_log_dir() / f"audit_{_safe_tenant_slug(entry['tenant_id'])}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            "audit_events: %s %s/%s risk=%s trace=%s",
            action,
            resource_type,
            resource_id,
            risk_level,
            entry["trace_id"],
        )
    except OSError as exc:
        logger.warning("audit_events: 审计落盘失败: %s", exc)
    return entry


def query(
    resource_id: str = "",
    resource_type: str = "",
    action: str = "",
    actor: str = "",
    trace_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """只读检索审计轨迹（逐行解析，缺字段兜底 — 对齐 audit_store._entry_from_line）。"""
    hits: list[dict[str, Any]] = []
    root = audit_log_dir()
    if not root.is_dir():
        return hits
    files = sorted(root.glob("audit_*.jsonl"), reverse=True)
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or not rec.get("action"):
                continue
            if resource_id and rec.get("resource_id") != resource_id:
                continue
            if resource_type and rec.get("resource_type") != resource_type:
                continue
            if action and rec.get("action") != action:
                continue
            if actor and rec.get("actor") != actor:
                continue
            if trace_id and rec.get("trace_id") != trace_id:
                continue
            hits.append(rec)
            if len(hits) >= limit:
                return hits
    return hits
