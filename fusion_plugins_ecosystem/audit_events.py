# Capability operation audit event stream (issue #43 PC-3).
# Hub install / uninstall / OAuth authorization / scope policy change
# events are written to the fusion-security audit pipeline JSONL sink
# (~/.fusion_security/audit/audit_<tenant>.jsonl, same format as
# fusion_security.engine.tenant.audit.AuditLogger — directly searchable by
# the security#61 audit workbench); trace_id strings thread the
# cross-service chain.
# Depends only on the file contract, does not import fusion_security
# (zero cross-project coupling).

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
    # Cross-service trace_id: fp-<first 16 hex of uuid4>.
    return "fp-" + uuid.uuid4().hex[:16]


def _safe_tenant_slug(tenant_id: str) -> str:
    slug = _TENANT_SAFE.sub("_", tenant_id or "").strip("._-")
    return slug or "system"


def audit_log_dir() -> Path:
    # Read env lazily — resolving at import time would defeat test
    # monkeypatching (events leaking into the real directory)
    return Path(
        os.environ.get(
            "FUSION_PLUGIN_AUDIT_DIR",
            str(Path.home() / ".fusion_security" / "audit"),
        )
    )


def _sink_enabled() -> bool:
    # Disable persisting (test/offline scenarios): FUSION_PLUGIN_AUDIT_SINK=off
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
    # Append one audit event (AuditEntry-shaped JSONL line).
    # Failure never takes down the business flow — audit persistence
    # degrades to a warning log.
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
        logger.warning("audit_events: audit persist failed: %s", exc)
    return entry


def query(
    resource_id: str = "",
    resource_type: str = "",
    action: str = "",
    actor: str = "",
    trace_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    # Read-only audit trail search (line-by-line parse, tolerant of missing
    # fields — aligned with audit_store._entry_from_line).
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
