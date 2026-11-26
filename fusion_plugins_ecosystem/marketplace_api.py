# Marketplace backend API (issue #74) — listing / detail / install-state /
# publish-review for the web-shell marketplace page. Single source of truth
# stays the plugin registry + marketplace index + bundles state; this module
# merges presentational metadata from builtin/marketplace_catalog.json and
# derives per-plugin permissions so the UI can surface access before install.

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fusion_plugins_ecosystem.extensions_console import (
    ExtensionsConsole,
    _extensions_dir,
)
from fusion_plugins_ecosystem.registry import PluginCapability, PluginCategory

logger = logging.getLogger(__name__)

CATALOG_FILE = Path(__file__).resolve().parent / "builtin" / "marketplace_catalog.json"

# persisted in the extensions dir (env FUSION_PLUGIN_EXTENSIONS_DIR overrides
# for tests — same isolation pattern as bundles.json / connectors.json)
TENANTS_FILE = "marketplace_tenants.json"
SUBMISSIONS_FILE = "marketplace_submissions.json"
COMMUNITY_FILE = "marketplace_community.json"

DEFAULT_TENANT = "local"
TENANT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")

INSTALL_STATES = ("not_installed", "installed", "update_available")
SUBMISSION_STATUSES = ("pending", "approved", "rejected")

# manifest capability → surfaced permission (issue #74: user must see what a
# plugin can access before installing)
CAPABILITY_PERMISSIONS: dict[str, dict[str, str]] = {
    PluginCapability.FILE_ACCESS.value: {
        "id": "file_access",
        "label_en": "Read and write local files",
        # i18n: user-facing
        "label_cn": "读写本地文件",
        "risk": "medium",
    },
    PluginCapability.MCP_TOOL.value: {
        "id": "network",
        "label_en": "Send network requests (exposed as MCP tool)",
        # i18n: user-facing
        "label_cn": "发起网络请求（暴露为 MCP 工具）",
        "risk": "medium",
    },
    PluginCapability.VRAM_CONSUMER.value: {
        "id": "vram",
        "label_en": "Reserve MLX vRAM via Desk scheduler",
        # i18n: user-facing
        "label_cn": "占用 MLX 显存（Desk 调度）",
        "risk": "low",
    },
    PluginCapability.LONG_TASK.value: {
        "id": "long_task",
        "label_en": "Run long tasks (timeout meltdown applies)",
        # i18n: user-facing
        "label_cn": "运行长任务（超时熔断保护）",
        "risk": "low",
    },
    PluginCapability.SUBAGENT.value: {
        "id": "subagent",
        "label_en": "Dispatch Claude Code sub-agents",
        # i18n: user-facing
        "label_cn": "调度 Claude Code 子代理",
        "risk": "medium",
    },
}

CONNECTOR_PERMISSION_MEDIUM = {
    "id": "network",
    "label_en": "Connect to an external MCP server (stdio/HTTP)",
    # i18n: user-facing
    "label_cn": "连接外部 MCP 服务器（stdio/HTTP）",
    "risk": "medium",
}
CONNECTOR_PERMISSION_HIGH = {
    "id": "network",
    "label_en": "Connect to an external MCP server (stdio/HTTP, HIGH-risk tool declared)",
    # i18n: user-facing
    "label_cn": "连接外部 MCP 服务器（stdio/HTTP，含 HIGH 风险工具）",
    "risk": "high",
}

_LIST_DEFAULT_ICON = "puzzle"


class PluginMarketplaceAPI:
    """Marketplace listing/detail/install-state/publish-review service."""

    def __init__(self, console: ExtensionsConsole) -> None:
        self.console = console

    # ── persistence ──

    @staticmethod
    def _read_json(name: str, default: Any) -> Any:
        path = _extensions_dir() / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("marketplace_api: %s corrupted, returning default", name)
            return default

    @staticmethod
    def _write_json(name: str, data: Any) -> None:
        d = _extensions_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / name)

    # ── metadata sources ──

    @staticmethod
    def _load_catalog() -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        try:
            raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("marketplace_api: catalog load failed: %s", exc)
            raw = {}
        for entry in raw.get("plugins", []):
            if isinstance(entry, dict) and entry.get("id"):
                catalog[str(entry["id"])] = entry
        # approved community submissions extend the catalog (community wins on
        # id collision only if absent from the built-in file)
        try:
            community = json.loads(
                (_extensions_dir() / COMMUNITY_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            community = {}
        for pid, manifest in (community.get("plugins") or {}).items():
            if pid in catalog:
                continue
            meta = dict(manifest or {})
            # submission manifests use plugin-manifest key names; the catalog
            # layer expects presentational keys — map them over
            if manifest:
                meta.setdefault("name_en", manifest.get("name") or pid)
                meta.setdefault("description_en", manifest.get("description") or "")
                meta["version"] = manifest.get("version") or "0.0.0"
                meta["permissions"] = manifest.get("permissions") or []
                meta["tags"] = manifest.get("tags") or []
            meta.setdefault("source_module", meta.get("source_repo") or "community")
            meta.setdefault("official", False)
            meta.setdefault("lifecycle_stage", "beta")
            catalog[str(pid)] = meta
        return catalog

    def _index_entries(self) -> list[Any]:
        try:
            from fusion_plugins_ecosystem.marketplace import Marketplace

            mp = Marketplace(
                connector_registry=None,
                marketplace_index=(
                    os.environ.get("FUSION_PLUGIN_MARKETPLACE_INDEX") or None
                ),
            )
            entries, warnings = mp.load_index()
            for w in warnings:
                logger.warning("marketplace_api: index warning: %s", w)
            return entries
        except Exception as exc:  # noqa: BLE001 — index unavailable must not kill listing
            logger.warning("marketplace_api: marketplace index unavailable: %s", exc)
            return []

    def _bundles_by_id(self) -> dict[str, dict[str, Any]]:
        state = self.console._bundles_state()
        return {k: v for k, v in state.items() if k != "_scope_policy"}

    # ── tenant install state ──

    @staticmethod
    def _normalize_tenant(tenant: str | None) -> str:
        t = str(tenant or DEFAULT_TENANT).strip() or DEFAULT_TENANT
        if not TENANT_RE.match(t):
            raise ValueError(f"marketplace_api: invalid tenant id {t!r}")
        return t

    def _tenants_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        return self._read_json(TENANTS_FILE, {})

    def _save_tenants_state(self, state: dict[str, Any]) -> None:
        self._write_json(TENANTS_FILE, state)

    def _tenant_record(
        self, tenants: dict[str, Any], tenant: str, plugin_id: str
    ) -> dict[str, Any] | None:
        return (tenants.get(tenant) or {}).get(plugin_id)

    def _record_install(
        self, tenant: str, plugin_id: str, version: str, enabled: bool
    ) -> None:
        tenants = self._tenants_state()
        tenants.setdefault(tenant, {})[plugin_id] = {
            "version": str(version),
            "installed_at": int(time.time()),
            "enabled": bool(enabled),
        }
        self._save_tenants_state(tenants)

    def _record_remove(self, tenant: str, plugin_id: str) -> None:
        tenants = self._tenants_state()
        changed = False
        if plugin_id in (tenants.get(tenant) or {}):
            del tenants[tenant][plugin_id]
            changed = True
        if changed:
            self._save_tenants_state(tenants)

    def _install_count(self, plugin_id: str, base: int) -> int:
        tenants = self._tenants_state()
        return int(base) + sum(1 for t in tenants.values() if plugin_id in (t or {}))

    def _state_for(
        self,
        plugin_id: str,
        available_version: str,
        tenant: str,
        tenants: dict[str, Any],
        is_registry_plugin: bool,
    ) -> dict[str, Any]:
        record = self._tenant_record(tenants, tenant, plugin_id)
        live_enabled = False
        if is_registry_plugin:
            try:
                live_enabled = self.console._plugin_enabled(plugin_id)
            except Exception:  # noqa: BLE001 — registry probe is best-effort
                live_enabled = False
        if record is None and not live_enabled:
            return {"install_state": "not_installed", "installed_version": ""}
        installed_version = str((record or {}).get("version") or "")
        if not installed_version:
            # live-enabled but never recorded through this API
            return {"install_state": "installed", "installed_version": ""}
        key = ExtensionsConsole._version_key
        if key(installed_version) < key(available_version or "0"):
            return {
                "install_state": "update_available",
                "installed_version": installed_version,
            }
        return {"install_state": "installed", "installed_version": installed_version}

    # ── listing / detail ──

    def _permission_for_connector_pack(self, entry: Any) -> list[dict[str, str]]:
        pack = getattr(entry, "pack", None) or {}
        tools = pack.get("tools") or []
        high = any(str((t or {}).get("risk", "")).upper() == "HIGH" for t in tools)
        return [
            dict(CONNECTOR_PERMISSION_HIGH if high else CONNECTOR_PERMISSION_MEDIUM)
        ]

    def _base_row(self, plugin_id: str, manifest: Any) -> dict[str, Any]:
        m = manifest
        caps = []
        try:
            caps = [c.value for c in m.capabilities]
        except AttributeError:
            caps = [str(c) for c in (m.get("capabilities") or [])] if m else []
        return {
            "id": plugin_id,
            "name_en": getattr(m, "name", None) or (m or {}).get("name") or plugin_id,
            "name_cn": "",
            "version": str(
                getattr(m, "version", None) or (m or {}).get("version") or "0.0.0"
            ),
            "category": str(
                getattr(m, "category", None)
                and getattr(m.category, "value", m.category)
                or (m or {}).get("category")
                or "custom"
            ),
            "description_en": getattr(m, "description", None)
            or (m or {}).get("description")
            or "",
            "description_cn": "",
            "tags": [],
            "icon": _LIST_DEFAULT_ICON,
            "installs": 0,
            "rating": None,
            "official": True,
            "lifecycle_stage": "stable",
            "source_module": "fusion-plugins-ecosystem",
            "kind": "plugin",
            "capabilities": caps,
        }

    def _apply_catalog(self, row: dict[str, Any], meta: dict[str, Any]) -> None:
        for key in (
            "name_en",
            "name_cn",
            "description_en",
            "description_cn",
            "category",
            "source_module",
            "source_repo",
            "icon",
            "lifecycle_stage",
            "version",
        ):
            if meta.get(key):
                row[key] = meta[key]
        for key in ("tags", "changelog", "permissions"):
            if meta.get(key) is not None:
                row[key] = meta[key]
        if meta.get("official") is not None:
            row["official"] = bool(meta["official"])
        if meta.get("rating") is not None:
            row["rating"] = meta["rating"]
        if meta.get("installs") is not None:
            row["installs"] = int(meta["installs"])
        if meta.get("size_kb") is not None:
            row["size_kb"] = meta["size_kb"]

    def list_plugins(
        self, tenant: str | None = None, query: str = "", category: str = ""
    ) -> dict[str, Any]:
        t = self._normalize_tenant(tenant)
        rows: dict[str, dict[str, Any]] = {}

        # registry plugins (bridged connector/mcp manifests stay out — the
        # connector packs themselves are the marketplace cards)
        for m in self.console.registry.list():
            if m.id.startswith(("connector__", "mcp__")):
                continue
            rows[m.id] = self._base_row(m.id, m)

        # marketplace index entries (connector packs / skill packages / bundles)
        bundles = self._bundles_by_id()
        for e in self._index_entries():
            if e.id in rows:
                row = rows[e.id]
            else:
                row = self._base_row(e.id, None)
                row["official"] = False
                row["lifecycle_stage"] = "beta"
                row["kind"] = e.kind
                row["version"] = str(e.version)
                row["source_module"] = str(e.source) if e.source else "builtin"
                row["description_en"] = e.description or ""
                rows[e.id] = row
            if e.kind == "connector_pack":
                row.setdefault("permissions", self._permission_for_connector_pack(e))
                row["kind"] = "connector_pack"
            if e.id in bundles:
                row["available_version"] = str(e.version)
                row.setdefault("changelog", [e.description] if e.description else [])

        # catalog metadata overrides (presentational only)
        catalog = self._load_catalog()
        for pid, meta in catalog.items():
            if pid in rows:
                self._apply_catalog(rows[pid], meta)
            else:
                # catalog-only entry (not yet in registry/index) — still shown
                row = self._base_row(pid, None)
                row["official"] = False
                row["lifecycle_stage"] = "beta"
                rows[pid] = row
                self._apply_catalog(row, meta)

        # install state per tenant
        tenants = self._tenants_state()
        out: list[dict[str, Any]] = []
        for pid, row in rows.items():
            meta = catalog.get(pid) or {}
            available = row.get("available_version") or row["version"]
            state = self._state_for(
                pid,
                available,
                t,
                tenants,
                is_registry_plugin=pid in {m.id for m in self.console.registry.list()},
            )
            row.update(state)
            row["installs"] = self._install_count(pid, row.get("installs") or 0)
            row.setdefault("permissions", [])
            row.setdefault("changelog", [])
            out.append(row)

        q = str(query or "").strip().lower()
        if q:
            out = [
                r
                for r in out
                if q in str(r.get("id", "")).lower()
                or q in str(r.get("name_en", "")).lower()
                or q in str(r.get("name_cn", "")).lower()
                or q in str(r.get("description_en", "")).lower()
                or any(q in str(tag).lower() for tag in (r.get("tags") or []))
            ]
        if category:
            out = [r for r in out if r.get("category") == category]
        out.sort(key=lambda r: (not r.get("official"), str(r["id"])))
        categories = sorted({str(r.get("category") or "custom") for r in out})
        return {
            "tenant": t,
            "count": len(out),
            "categories": categories,
            "plugins": out,
        }

    def plugin_detail(
        self, plugin_id: str, tenant: str | None = None
    ) -> dict[str, Any]:
        t = self._normalize_tenant(tenant)
        listing = self.list_plugins(tenant=t)
        row = next((r for r in listing["plugins"] if r["id"] == plugin_id), None)
        if row is None:
            raise KeyError(f"marketplace_api: unknown plugin {plugin_id!r}")

        manifest = self.console.registry.get(plugin_id)
        permissions: list[dict[str, str]] = list(row.get("permissions") or [])
        if manifest is not None:
            derived = [
                CAPABILITY_PERMISSIONS[c.value]
                for c in manifest.capabilities
                if c.value in CAPABILITY_PERMISSIONS
            ]
            seen = {p["id"] for p in permissions}
            permissions = permissions + [p for p in derived if p["id"] not in seen]
        row["permissions"] = permissions

        size_kb = row.get("size_kb")
        if size_kb is None:
            size_kb = int(getattr(manifest, "vram_mb", 0) or 0) or None
        row["size_kb"] = size_kb
        row["params"] = (
            [p.to_dict() for p in manifest.params] if manifest is not None else []
        )
        row["tenant"] = t
        return row

    # ── install / uninstall (idempotent, per-tenant record) ──

    def _market_entry_map(self) -> dict[str, Any]:
        return {e.id: e for e in self._index_entries()}

    async def install(
        self, plugin_id: str, tenant: str | None = None
    ) -> dict[str, Any]:
        t = self._normalize_tenant(tenant)
        listing = self.list_plugins(tenant=t)
        row = next((r for r in listing["plugins"] if r["id"] == plugin_id), None)
        if row is None:
            raise KeyError(f"marketplace_api: unknown plugin {plugin_id!r}")

        available = row.get("available_version") or row["version"]
        record = self._tenant_record(self._tenants_state(), t, plugin_id)
        if (
            record is not None
            and row["install_state"] == "installed"
            and str(record.get("version")) == str(available)
        ):
            logger.info("marketplace_api: %s already installed for %s", plugin_id, t)
            return {
                "installed": True,
                "already": True,
                "plugin_id": plugin_id,
                "version": available,
                "tenant": t,
            }

        market_entries = self._market_entry_map()
        if plugin_id in market_entries:
            result = await self.console.install_bundle(plugin_id)
            version = str(result.get("version") or available)
            self._record_install(t, plugin_id, version, enabled=True)
            action = (
                "update" if row["install_state"] == "update_available" else "install"
            )
            logger.info(
                "marketplace_api: %s %s v%s (tenant %s)", action, plugin_id, version, t
            )
            return {
                "installed": True,
                "already": False,
                "plugin_id": plugin_id,
                "version": version,
                "tenant": t,
                "action": action,
                "items": result.get("items"),
                "errors": result.get("errors"),
            }

        manifest = self.console.registry.get(plugin_id)
        if manifest is None:
            raise KeyError(
                f"marketplace_api: plugin {plugin_id!r} has no installable source"
            )
        await self.console.lifecycle.enable(plugin_id)
        self._record_install(t, plugin_id, str(manifest.version), enabled=True)
        from fusion_plugins_ecosystem.audit_events import emit, new_trace_id

        trace = new_trace_id()
        emit(
            "marketplace.plugin.install",
            resource_type="plugin",
            resource_id=plugin_id,
            details={"version": manifest.version, "tenant": t},
            risk_level="low",
            trace_id=trace,
        )
        logger.info("marketplace_api: plugin %s enabled (tenant %s)", plugin_id, t)
        return {
            "installed": True,
            "already": False,
            "plugin_id": plugin_id,
            "version": str(manifest.version),
            "tenant": t,
            "action": "install",
        }

    async def uninstall(
        self, plugin_id: str, tenant: str | None = None
    ) -> dict[str, Any]:
        t = self._normalize_tenant(tenant)
        market_entries = self._market_entry_map()
        had_record = (
            self._tenant_record(self._tenants_state(), t, plugin_id) is not None
        )
        if plugin_id in market_entries:
            await self.console.uninstall_bundle(plugin_id)
        else:
            manifest = self.console.registry.get(plugin_id)
            if manifest is None and not had_record:
                raise KeyError(f"marketplace_api: unknown plugin {plugin_id!r}")
            if manifest is not None:
                await self.console.lifecycle.disable(plugin_id)
        self._record_remove(t, plugin_id)
        from fusion_plugins_ecosystem.audit_events import emit, new_trace_id

        emit(
            "marketplace.plugin.uninstall",
            resource_type="plugin" if plugin_id not in market_entries else "bundle",
            resource_id=plugin_id,
            details={"tenant": t},
            risk_level="low",
            trace_id=new_trace_id(),
        )
        logger.info("marketplace_api: %s uninstalled (tenant %s)", plugin_id, t)
        return {
            "uninstalled": True,
            "already": not had_record,
            "plugin_id": plugin_id,
            "tenant": t,
        }

    # ── publish / review queue (issue #74 item 4) ──

    @staticmethod
    def _validate_submission(manifest: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        pid = str(manifest.get("id") or "")
        if not PLUGIN_ID_RE.match(pid):
            problems.append(f"invalid plugin id {pid!r}")
        for field in ("name", "version", "description"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"missing required field {field!r}")
        version = str(manifest.get("version") or "")
        if version and not VERSION_RE.match(version):
            problems.append(f"invalid version {version!r}")
        category = manifest.get("category")
        valid_categories = {c.value for c in PluginCategory}
        if category not in valid_categories:
            problems.append(
                f"invalid category {category!r} (one of {sorted(valid_categories)})"
            )
        permissions = manifest.get("permissions")
        if permissions is not None:
            if not isinstance(permissions, list) or not all(
                isinstance(p, dict) and p.get("id") for p in permissions
            ):
                problems.append("permissions must be a list of {id, ...} objects")
        source_repo = manifest.get("source_repo")
        if source_repo is not None and not str(source_repo).startswith("https://"):
            problems.append("source_repo must be an https:// URL")
        return problems

    def submit(
        self, manifest: dict[str, Any], submitter: str = "anonymous"
    ) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        problems = self._validate_submission(manifest)
        if problems:
            raise ValueError(f"manifest validation failed: {problems}")
        pid = str(manifest["id"])
        version = str(manifest["version"])
        community = self._read_json(COMMUNITY_FILE, {"plugins": {}})
        published = (community.get("plugins") or {}).get(pid) or {}
        if str(published.get("version") or "") == version:
            raise ValueError(f"plugin {pid!r} version {version} is already published")
        submissions = self._read_json(SUBMISSIONS_FILE, {"submissions": []})
        submission_id = f"{pid}@{version}"
        existing = next(
            (
                s
                for s in submissions["submissions"]
                if s["submission_id"] == submission_id
            ),
            None,
        )
        if existing is not None and existing["status"] == "approved":
            raise ValueError(f"submission {submission_id} already approved")
        record = {
            "submission_id": submission_id,
            "plugin_id": pid,
            "version": version,
            "status": "pending",
            "submitter": str(submitter),
            "submitted_at": int(time.time()),
            "reviewed_at": None,
            "reviewer": None,
            "manifest": manifest,
        }
        if existing is not None:
            submissions["submissions"].remove(existing)
        submissions["submissions"].append(record)
        self._write_json(SUBMISSIONS_FILE, submissions)
        from fusion_plugins_ecosystem.audit_events import emit, new_trace_id

        emit(
            "marketplace.submission.create",
            resource_type="submission",
            resource_id=submission_id,
            details={"submitter": submitter, "resubmission": existing is not None},
            risk_level="low",
            trace_id=new_trace_id(),
        )
        logger.info("marketplace_api: submission %s pending", submission_id)
        return {"submitted": True, "submission_id": submission_id, "status": "pending"}

    def list_submissions(self, status: str = "") -> dict[str, Any]:
        data = self._read_json(SUBMISSIONS_FILE, {"submissions": []})
        subs = data.get("submissions") or []
        if status:
            if status not in SUBMISSION_STATUSES:
                raise ValueError(f"invalid status filter {status!r}")
            subs = [s for s in subs if s.get("status") == status]
        return {"count": len(subs), "submissions": subs}

    def review(
        self, submission_id: str, approve: bool, reviewer: str = "admin"
    ) -> dict[str, Any]:
        submissions = self._read_json(SUBMISSIONS_FILE, {"submissions": []})
        record = next(
            (
                s
                for s in submissions.get("submissions") or []
                if s.get("submission_id") == submission_id
            ),
            None,
        )
        if record is None:
            raise KeyError(f"marketplace_api: unknown submission {submission_id!r}")
        if record.get("status") != "pending":
            raise ValueError(
                f"submission {submission_id} already {record.get('status')}"
            )
        record["status"] = "approved" if approve else "rejected"
        record["reviewed_at"] = int(time.time())
        record["reviewer"] = str(reviewer)
        self._write_json(SUBMISSIONS_FILE, submissions)
        if approve:
            community = self._read_json(COMMUNITY_FILE, {"plugins": {}})
            plugins = community.setdefault("plugins", {})
            existing = plugins.get(record["plugin_id"])
            if existing is None or ExtensionsConsole._version_key(
                str(record["version"])
            ) >= ExtensionsConsole._version_key(str(existing.get("version") or "0")):
                plugins[record["plugin_id"]] = dict(record["manifest"])
                self._write_json(COMMUNITY_FILE, community)
        from fusion_plugins_ecosystem.audit_events import emit, new_trace_id

        emit(
            "marketplace.submission.review",
            resource_type="submission",
            resource_id=submission_id,
            details={"approve": approve, "reviewer": reviewer},
            risk_level="medium",
            trace_id=new_trace_id(),
        )
        logger.info(
            "marketplace_api: submission %s %s by %s",
            submission_id,
            record["status"],
            reviewer,
        )
        return {
            "reviewed": True,
            "submission_id": submission_id,
            "status": record["status"],
        }
