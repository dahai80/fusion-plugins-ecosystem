# Extensions console backend (issue #30 E3: Connectors + Plugins·MCP tabs).
# Generalized from f-ecommerce agent_platform/extensions.py + mcp_intake.py +
# plugin_bridge.py with all e-commerce semantics stripped:
# - Connectors: declarative JSON connector packs (rest_connector.py). install =
#   full static validation + persist to disk; enable = runtime bridge of read
#   channels (GET) into `connector__<id>__<op>` capability plugins (MCP_TOOL,
#   INLINE). Write channels are not auto-listed and enter a manual review
#   queue. Credentials are stored encrypted via Desk (Fernet), resolved
#   server-side at call time, never exposed to the agent. harness = static
#   validation + a live check of the first read op using vault credentials.
# - Plugins·MCP: plugin tick/untick (lifecycle enable/disable reconciliation),
#   declarative plugin pack install (plugin.json manifest, no arbitrary code
#   on disk), remote MCP server probe & register (initialize + tools/list);
#   ticked tools bridge into `mcp__<server>__<tool>` capability plugins
#   dispatched via JSON-RPC tools/call.
# Capability spine: derived from on-disk facts (connector docs + MCP ticks +
# plugin ticks); resync rebuilds bridges at startup — no in-memory second
# source of truth.

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any

from fusion_plugins_ecosystem import audit_events
from fusion_plugins_ecosystem.config import DATA_DIR
from fusion_plugins_ecosystem.rest_connector import (
    connector_report,
    delete_connector,
    execute_operation,
    get_connector,
    read_channels,
    save_connector,
    validate_connector,
    write_channels,
)

logger = logging.getLogger(__name__)

DEFAULT_EXTENSIONS_DIR = str(DATA_DIR / "extensions")  # issue #38: single-point data dir resolution
_MCP_KEY = "mcp_servers.json"
_TICKS_KEY = "plugin_ticks.json"
_PLUGINS_KEY = "plugins"
_MCP_TIMEOUT = 15.0


def _extensions_dir() -> Path:
    return Path(os.environ.get("FUSION_PLUGIN_EXTENSIONS_DIR", DEFAULT_EXTENSIONS_DIR)).expanduser()


_VAULT_FILE = "vault.json"


# Console-local encrypted vault (fallback storage when Desk config_center is absent).
def _local_vault_read() -> dict[str, Any]:
    path = _extensions_dir() / _VAULT_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("extensions_console: local vault corrupt, treating as empty")
        return {}


def _local_vault_write(data: dict[str, Any]) -> None:
    d = _extensions_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _VAULT_FILE
    tmp = d / (_VAULT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    path.chmod(0o600)


# Resolve credential JSON from Desk encrypted storage; return an empty dict
# when missing (caller fails loud).
# Falls back to the console-local vault when Desk config_center is absent
# (standalone console).
def _vault_credentials(desk: Any, credential_ref: str) -> dict[str, Any]:
    if not credential_ref:
        return {}
    raw = desk.get_api_key(credential_ref) if desk else None
    if not raw:
        stored = _local_vault_read().get(credential_ref)
        if stored:
            from fusion_plugins_ecosystem.desk_runtime import _decrypt_key

            raw = _decrypt_key(str(stored))
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"api_key": str(parsed)}
    except ValueError:
        return {"api_key": raw}


# Vault credentials -> remote MCP server auth headers.
# Accepts {"headers": {...}} injected as-is, or {"api_key"|"token"} -> Bearer.
# Non-empty ref with nothing stored -> fail loud (precise probe-time error, no
# half registration).
def _vault_headers(desk: Any, credential_ref: str) -> dict[str, str]:
    if not credential_ref:
        return {}
    creds = _vault_credentials(desk, credential_ref)
    if not creds:
        raise RuntimeError(f"no vault credentials under '{credential_ref}' — save them via PUT /api/adapters first")
    if isinstance(creds.get("headers"), dict):
        return {str(k): str(v) for k, v in creds["headers"].items()}
    key = creds.get("api_key") or creds.get("token")
    if key:
        return {"Authorization": f"Bearer {key}"}
    raise RuntimeError(
        f"vault credentials under '{credential_ref}' have neither "
        "'headers' nor 'api_key'/'token' — cannot build MCP auth headers"
    )


def _mcp_rpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = _MCP_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    # One JSON-RPC round trip against a streamable-http MCP endpoint.
    # trust_env=False: proxy env vars in the daemon shell hang loopback calls.
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    import httpx  # console extra — function-level import so the module imports without httpx in the base env

    resp = httpx.post(
        url,
        json=payload,
        timeout=timeout,
        trust_env=False,
        headers=headers or {},
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error']}")
    return body.get("result") or {}


# Declarative plugin.json manifest -> PluginManifest (entry_point must be module:attr).
def _manifest_from_decl(data: dict[str, Any]) -> Any:
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
    )
    from fusion_plugins_ecosystem.schema import SandboxMode

    ep = data.get("entry_point")
    if not isinstance(ep, str) or ":" not in ep:
        raise ValueError("declarative plugin requires entry_point 'module:attr'")
    try:
        category = PluginCategory(data.get("category", "custom"))
    except ValueError:
        category = PluginCategory.CUSTOM
    caps = tuple(
        PluginCapability(c) for c in (data.get("capabilities") or []) if c in PluginCapability._value2member_map_
    )
    return PluginManifest(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        version=str(data.get("version", "0.0.0")),
        category=category,
        description=str(data.get("description", "")),
        capabilities=caps,
        entry_point=ep,
        default_mounted=bool(data.get("default_mounted", False)),
        sandbox_mode=SandboxMode(data.get("sandbox_mode", "inline")),
    )


# REST read-op bound entry point: closure -> execute_operation (INLINE only).
class _RestOpBound:
    def __init__(self, connector_id: str, op_name: str, op: dict[str, Any]) -> None:
        self.connector_id = connector_id
        self.op_name = op_name
        self.op = op

    async def __call__(self, desk: Any, params: dict[str, Any]) -> Any:
        creds = _vault_credentials(desk, self.connector_id)
        result = await execute_operation(
            get_connector(self.connector_id) or {},
            self.op_name,
            credentials=creds,
            variables=dict(params or {}),
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"connector {self.connector_id}.{self.op_name} failed: "
                f"{result.get('error_code')}: {result.get('reason')}"
            )
        return result


# MCP intake tool bound entry point: closure -> tools/call (INLINE only).
class _McpToolBound:
    def __init__(self, console: ExtensionsConsole, server_id: str, tool: str) -> None:
        self.console = console
        self.server_id = server_id
        self.tool = tool

    async def __call__(self, desk: Any, params: dict[str, Any]) -> Any:
        result = await self.console.call_mcp_tool(self.server_id, self.tool, dict(params or {}))
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "MCP tools/call failed")
        return result.get("result") or {}


# Extensions console service (connectors + plugins + MCP intake + spine derivation).
class ExtensionsConsole:
    def __init__(
        self,
        desk: Any = None,
        registry: Any = None,
        lifecycle: Any = None,
        installed_by: str = "console",
    ) -> None:
        from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
        from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
        from fusion_plugins_ecosystem.registry import PluginRegistry

        self.desk = desk or DeskRuntime()
        self.registry = registry or PluginRegistry(self.desk)
        # standalone console builds its own lifecycle when not injected
        # (tick/enable bridging depends on it)
        self.lifecycle = lifecycle or PluginLifecycle(self.registry)
        self.installed_by = installed_by
        # issue #41: the hub surface reflects the real registry — console and
        # fusion-plugin-server share the same source, so bootstrap builtin
        # plugins here (register is idempotent for same version; no-op when
        # the injection side already registered)
        try:
            self.registry.register_builtin()
        except ValueError as exc:
            logger.warning("extensions_console: builtin plugin registration conflict: %s", exc)

    # ── persistence ──

    def _read_json(self, name: str, default: Any) -> Any:
        path = _extensions_dir() / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("extensions_console: %s corrupt, returning default", name)
            return default

    def _write_json(self, name: str, data: Any) -> None:
        d = _extensions_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / name)

    # ── capability spine (derived, no second source of truth) ──

    def list_capabilities(self) -> list[dict[str, Any]]:
        caps: list[dict[str, Any]] = []
        for entry in connector_report():
            cid = entry["id"]
            doc = get_connector(cid) or {}
            disabled = bool(doc.get("_disabled"))
            for ch in read_channels(doc):
                # The spine is driven by runtime bridge facts: only connectors
                # that are enabled (bridge exists) or were enabled then
                # disabled (_disabled marker) are listed
                bridged = self.registry.get(f"connector__{cid}__{ch['id']}")
                if not bridged and not disabled:
                    continue
                caps.append(
                    {
                        "id": f"connector.{cid}.{ch['id']}",
                        "title": ch["title"],
                        "description": ch["description"],
                        "source": "connector",
                        "status": "disabled" if disabled else "active",
                        "route": {"connector": cid, "op": ch["id"]},
                    }
                )
        for server in self.list_mcp_servers():
            for tool in server.get("ticked", []):
                caps.append(
                    {
                        "id": f"mcp.{server['id']}.{tool}",
                        "title": tool,
                        "description": f"{tool} via MCP server {server['id']}",
                        "source": "mcp",
                        "status": "active",
                        "route": {"mcp_server": server["id"], "tool": tool},
                    }
                )
        for m in self.registry.list():
            if m.id.startswith(("connector__", "mcp__")):
                continue  # bridge manifests are covered by the connector/mcp sources
            if self._plugin_enabled(m.id):
                caps.append(
                    {
                        "id": f"node.{m.id}",
                        "title": m.name,
                        "description": m.description or m.id,
                        "source": "node",
                        "status": "active",
                        "route": {"plugin": m.id},
                    }
                )
        return caps

    # ── capability hub (issue #41: four tabs + groups + cross-type search) ──

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        # Semantic version comparison key: '1.2' < '1.10', '1.0' == '1.0.0',
        # non-numeric segments count as 0.
        parts = []
        for chunk in str(version or "0").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()  # normalize trailing zeros: 1.0.0 == 1.0
        return tuple(parts) or (0,)

    # Marketplace catalog -> (updates, recommended): installed entries are
    # version-compared, uninstalled ones land in the recommended slot.
    # Read-only catalog (Marketplace(None)) — the install action belongs to
    # PC-2; when the marketplace is unavailable the hub degrades to empty
    # groups without dragging down the four tabs.
    def _market_groups(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            from fusion_plugins_ecosystem.marketplace import Marketplace

            mp = Marketplace(connector_registry=None)
            entries, warnings = mp.load_index()
            # Read-only catalog (registry=None): list_installed would go
            # through connector_registry.list_packs() — degrade to skill
            # package dir scan + connector docs when None
            installed: dict[str, dict[str, Any]] = {}
            if mp.connector_registry is not None:
                installed = {i["id"]: i for i in mp.list_installed()}
            elif os.path.isdir(mp.install_root):
                for name in sorted(os.listdir(mp.install_root)):
                    path = os.path.join(mp.install_root, name)
                    if not name.startswith("_") and os.path.isdir(path):
                        installed[name] = {"id": name}
        except Exception as exc:  # noqa: BLE001 — marketplace unavailable must not break the hub
            logger.warning("extensions_console: marketplace catalog unavailable: %s", exc)
            return [], []
        for entry in connector_report():
            installed.setdefault(entry["id"], entry)
        updates: list[dict[str, Any]] = []
        recommended: list[dict[str, Any]] = []
        for e in entries:
            if e.id in installed:
                current = str(installed[e.id].get("version") or "")
                if current and self._version_key(current) < self._version_key(e.version):
                    updates.append(
                        {
                            "id": e.id,
                            "name": e.name,
                            "kind": e.kind,
                            "installed_version": current,
                            "available_version": e.version,
                            "changelog": e.description,
                        }
                    )
            else:
                recommended.append(
                    {
                        "id": e.id,
                        "name": e.name,
                        "kind": e.kind,
                        "version": e.version,
                        "description": e.description,
                    }
                )
        if warnings:
            logger.info("extensions_console: marketplace index warnings: %s", warnings)
        return updates, recommended

    # Capability hub aggregate view: four tabs (plugins/skills/connectors/
    # subagents) + installed/updates/recommended groups + cross-type search
    # (id/name/description substring, case-insensitive).
    def list_hub(self, query: str = "") -> dict[str, Any]:
        q = str(query or "").strip().lower()

        def _match(*fields: Any) -> bool:
            return not q or any(q in str(f or "").lower() for f in fields)

        tabs: dict[str, list[dict[str, Any]]] = {
            "plugins": [],
            "skills": [],
            "connectors": [],
            "subagents": [],
        }
        installed: list[dict[str, Any]] = []
        for m in self.registry.list():
            if m.id.startswith(("connector__", "mcp__")):
                continue  # bridge manifests are covered by the connectors tab
            caps = [c.value for c in m.capabilities]
            enabled = self._plugin_enabled(m.id)
            base = {
                "id": m.id,
                "name": m.name,
                "version": m.version,
                "description": m.description or "",
                "category": m.category.value,
                "capabilities": caps,
                "enabled": enabled,
            }
            if _match(m.id, m.name, m.description):
                tabs["plugins"].append(base)
            if "claude_skill" in caps and _match(m.id, m.name, m.description):
                tabs["skills"].append({**base, "type": "skill"})
            if "subagent" in caps and _match(m.id, m.name, m.description):
                tabs["subagents"].append({**base, "type": "subagent"})
            installed.append({"id": m.id, "name": m.name, "type": "plugin", "enabled": enabled})
        for entry in connector_report():
            doc = get_connector(entry["id"]) or {}
            disabled = bool(doc.get("_disabled"))
            item = {
                "id": entry["id"],
                "name": entry["id"],
                "version": "",
                "description": entry.get("kind") or "",
                "kind": entry.get("kind") or "",
                "enabled": not disabled,
                "channels": entry.get("operations") or [],
            }
            if _match(item["id"], item["description"]):
                tabs["connectors"].append({**item, "type": "connector"})
            installed.append(
                {
                    "id": entry["id"],
                    "name": entry["id"],
                    "type": "connector",
                    "enabled": item["enabled"],
                }
            )
        for server in self.list_mcp_servers():
            item = {
                "id": server["id"],
                "name": server["id"],
                "version": "",
                "description": server.get("url", ""),
                "kind": "mcp",
                "enabled": bool(server.get("ticked")),
                "ticked": server.get("ticked") or [],
            }
            if _match(item["id"], item["description"]):
                tabs["connectors"].append({**item, "type": "mcp"})
            installed.append(
                {
                    "id": server["id"],
                    "name": server["id"],
                    "type": "mcp",
                    "enabled": item["enabled"],
                }
            )
        updates, recommended = self._market_groups()
        if q:
            # Cross-type search covers the recommended slot: uninstalled
            # marketplace entries also match (connector packs -> connectors
            # tab, skill packages -> skills tab), tagged recommended.
            for e in recommended:
                if not _match(e.get("id"), e.get("name"), e.get("description")):
                    continue
                item = {
                    "id": e["id"],
                    "name": e["name"],
                    "version": e.get("version") or "",
                    "description": e.get("description") or "",
                    "kind": e.get("kind") or "",
                    "enabled": False,
                    "recommended": True,
                }
                if e.get("kind") == "skill":
                    tabs["skills"].append({**item, "type": "skill"})
                else:
                    tabs["connectors"].append({**item, "type": "connector"})
        for key in tabs:
            tabs[key].sort(key=lambda x: str(x["id"]))
        installed.sort(key=lambda x: (x["type"], str(x["id"])))
        return {
            "query": str(query or ""),
            "tabs": tabs,
            "counts": {k: len(v) for k, v in tabs.items()},
            "groups": {
                "installed": installed,
                "updates": updates,
                "recommended": recommended,
            },
        }

    # ── install/authorize/update/uninstall flows (issue #42 PC-2) ──

    _BUNDLES_KEY = "bundles.json"

    def _bundles_state(self) -> dict[str, Any]:
        return self._read_json(self._BUNDLES_KEY, {})

    def _save_bundles_state(self, state: dict[str, Any]) -> None:
        self._write_json(self._BUNDLES_KEY, state)

    def _market_entry(self, entry_id: str) -> Any:
        from fusion_plugins_ecosystem.marketplace import Marketplace

        # Index source: FUSION_PLUGIN_MARKETPLACE_INDEX (local JSON / HTTPS);
        # builtin first-party catalog only when unset
        mp = Marketplace(
            connector_registry=None,
            marketplace_index=os.environ.get("FUSION_PLUGIN_MARKETPLACE_INDEX") or None,
        )
        entries, _ = mp.load_index()
        entry = next((e for e in entries if e.id == entry_id), None)
        if entry is None:
            raise KeyError(f"no marketplace entry {entry_id!r}")
        return entry

    @staticmethod
    def _entry_items(entry: Any) -> list[dict[str, Any]]:
        # Marketplace entry -> bundle items: bundles expand their declared
        # items; other packs become a single item.
        if entry.kind == "bundle":
            return list(entry.items or [])
        if entry.kind == "connector_pack":
            return [{"type": "connector_pack", "ref": entry.id}]
        return [{"type": "skill_package", "ref": entry.id}]

    # Inline connector registry used when bundles install connector_pack items
    # (lazily built).
    def _connector_registry(self) -> Any:
        from fusion_plugins_ecosystem.connector import ConnectorRegistry

        return ConnectorRegistry(
            desk=self.desk,
            registry=self.registry,
            sandbox_default_mode="inline",
        )

    # One-click marketplace bundle install (issue #42): skills enabled,
    # subagents registered + enabled, connectors persisted as pending_auth
    # (exposed after OAuth), connector_pack/skill_package via existing install
    # paths. Per-item state is persisted; items can be disabled individually.
    async def install_bundle(self, entry_id: str) -> dict[str, Any]:
        entry = self._market_entry(entry_id)
        state = self._bundles_state()
        items_out: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in self._entry_items(entry):
            itype = str(item.get("type"))
            ref = str(item.get("ref"))
            try:
                item_state = await self._install_bundle_item(itype, ref, item)
                items_out.append({"type": itype, "ref": ref, **item_state})
            except Exception as exc:  # noqa: BLE001 — per-item errors do not abort the bundle
                errors.append(f"{itype}:{ref}: {exc}")
                items_out.append({"type": itype, "ref": ref, "state": "error", "error": str(exc)})
        state[entry.id] = {
            "id": entry.id,
            "name": entry.name,
            "version": entry.version,
            "changelog": entry.description,
            "kind": entry.kind,
            "items": items_out,
        }
        self._save_bundles_state(state)
        # issue #43: install event into the audit pipeline (trace_id ties the bundle)
        trace = audit_events.new_trace_id()
        audit_events.emit(
            "hub.bundle.install",
            resource_type="bundle",
            resource_id=entry.id,
            details={
                "version": entry.version,
                "items": [{"type": i["type"], "ref": i["ref"], "state": i["state"]} for i in items_out],
                "errors": errors,
            },
            risk_level="medium",
            trace_id=trace,
        )
        for item in items_out:
            if item.get("state") != "error":
                audit_events.emit(
                    "hub.bundle.item.install",
                    resource_type=item["type"],
                    resource_id=str(item["ref"]),
                    details={"bundle": entry.id, "state": item["state"]},
                    risk_level="low",
                    trace_id=trace,
                )
        self.desk.log(
            entry.id,
            "INFO",
            f"bundle {entry.id} v{entry.version} installed ({len(items_out)} items, {len(errors)} errors)",
        )
        return {
            "installed": True,
            "bundle_id": entry.id,
            "version": entry.version,
            "items": items_out,
            "errors": errors,
        }

    async def _install_bundle_item(self, itype: str, ref: str, item: dict[str, Any]) -> dict[str, Any]:
        if itype in ("skill", "subagent"):
            manifest = self.registry.get(ref)
            if manifest is None:
                raise KeyError(f"plugin {ref!r} not registered (install_plugin first)")
            await self.lifecycle.enable(ref)
            return {"state": "enabled", "name": manifest.name}
        if itype == "connector":
            doc = dict(item.get("doc") or {})
            doc.setdefault("id", ref)
            problems = validate_connector(doc)
            if problems:
                raise ValueError(f"connector {ref} validation failed: {problems}")
            doc.pop("_disabled", None)
            save_connector(ref, doc)
            creds = _vault_credentials(self.desk, str(doc.get("credential_ref") or ref))
            if creds:
                # Credentials ready -> directly usable (equivalent to the #30
                # validate -> credentials -> enable path)
                await self.enable_connector(ref)
                return {"state": "enabled", "connector_id": ref}
            return {"state": "pending_auth", "connector_id": ref}
        if itype == "connector_pack":
            creg = self._connector_registry()
            pack = next((p for p in creg.list_packs() if p.id == ref), None)
            if pack is None:
                mp_entry = self._market_entry(ref)
                from fusion_plugins_ecosystem.connector import ConnectorPack

                pack_dict = mp_entry.pack
                if pack_dict is None:
                    raise ValueError(f"connector pack {ref} has no inline pack definition")
                pack = ConnectorPack.from_dict(pack_dict)
            plugin_ids = creg.install_pack(pack)
            # Bridge = expose: install path matches tick-on, tools set ENABLED
            for pid in plugin_ids:
                try:
                    await self.lifecycle.enable(pid)
                except Exception as exc:  # noqa: BLE001 — a single tool failure does not abort the pack
                    logger.warning("extensions_console: failed to enable pack tool %s: %s", pid, exc)
            self.desk.log(ref, "INFO", f"connector pack {ref} installed ({len(plugin_ids)} tools)")
            return {"state": "enabled", "plugin_ids": plugin_ids}
        if itype == "skill_package":
            entry = self._market_entry(ref)
            from fusion_plugins_ecosystem.marketplace import Marketplace

            mp = Marketplace(connector_registry=self._connector_registry())
            out = mp._install_skill_package(entry)
            return {"state": "enabled", "path": out.get("path")}
        raise ValueError(f"unknown bundle item type {itype!r}")

    def list_bundles(self) -> dict[str, Any]:
        state = self._bundles_state()
        # _scope_policy is a metadata key, not a bundle — excluded from the list
        return {"bundles": [v for k, v in state.items() if k != "_scope_policy"]}

    # Per-item enable/disable inside a bundle (issue #42: packed items can be
    # disabled individually after install).
    async def tick_bundle_item(self, bundle_id: str, ref: str, enabled: bool) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"ticked": False, "error": f"bundle {bundle_id!r} not installed"}
        item = next((i for i in bundle.get("items", []) if i.get("ref") == ref), None)
        if item is None:
            return {"ticked": False, "error": f"bundle {bundle_id} has no item {ref!r}"}
        if item.get("state") == "pending_auth" and enabled:
            return {"ticked": False, "error": f"{ref} pending authorization (complete OAuth first)"}
        try:
            if item["type"] in ("skill", "subagent"):
                if enabled:
                    await self.lifecycle.enable(ref)
                else:
                    await self.lifecycle.disable(ref)
            elif item["type"] == "connector":
                if enabled:
                    await self.enable_connector(ref)
                else:
                    await self.disable_connector(ref)
            elif item["type"] == "connector_pack":
                for pid in self._pack_plugin_ids(ref):
                    if enabled:
                        await self.lifecycle.enable(pid)
                    else:
                        await self.lifecycle.disable(pid)
            elif item["type"] == "skill_package":
                pass  # directory-style skill package has no runtime switch — state is bookkeeping only
            item["state"] = "enabled" if enabled else "disabled"
            self._save_bundles_state(state)
            self.desk.log(
                bundle_id,
                "INFO",
                f"bundle item {ref} -> {'enabled' if enabled else 'disabled'}",
            )
            audit_events.emit(
                "hub.bundle.item.enable" if enabled else "hub.bundle.item.disable",
                resource_type=item["type"],
                resource_id=ref,
                details={"bundle": bundle_id},
                risk_level="low",
            )
            return {"ticked": True, "ref": ref, "state": item["state"]}
        except Exception as exc:  # noqa: BLE001 — disable failures reported as-is
            return {"ticked": False, "error": str(exc)}

    def _pack_plugin_ids(self, pack_id: str) -> list[str]:
        return [m.id for m in self.registry.list() if m.id.startswith(f"connector__{pack_id}__")]

    # ── connector OAuth (issue #42: completed inside the hub, no redirects) ──

    # OAuth first screen: read scopes (tickable grants) + sensitive scopes
    # (per-use confirmation, never auto-opened).
    def oauth_scopes(self, connector_id: str) -> dict[str, Any]:
        doc = get_connector(connector_id)
        if not doc:
            return {"error": "connector not found"}
        # issue #43: annotate the actual grant state (_scope_grants absent =
        # full grant, compatible with the #30 direct-install path)
        grants = doc.get("_scope_grants")
        scopes = [
            {
                "id": ch["id"],
                "title": ch["title"],
                "sensitive": False,
                "granted": not isinstance(grants, list) or ch["id"] in grants,
            }
            for ch in read_channels(doc)
        ]
        sensitive = [{"id": wid, "title": wid, "sensitive": True} for wid in write_channels(doc)]
        auth = doc.get("auth") or {}
        fields = auth.get("credential_fields") or []
        return {
            "connector_id": connector_id,
            "scopes": scopes + sensitive,
            "credential_fields": fields,
        }

    async def oauth_complete(self, connector_id: str, scopes: list[str], credentials: dict[str, Any]) -> dict[str, Any]:
        # OAuth completion: credentials go into the vault, ticked read scopes
        # are granted + bridged, write scopes get a per_use policy (bound to
        # the cowork approval system, upstream fusion-cowork#225).
        doc = get_connector(connector_id)
        if not doc:
            return {"completed": False, "error": "connector not found"}
        if not isinstance(credentials, dict) or not credentials:
            return {"completed": False, "error": "credentials required"}
        valid_reads = {ch["id"] for ch in read_channels(doc)}
        valid_writes = set(write_channels(doc))
        unknown = [s for s in scopes if s not in valid_reads]
        if unknown:
            return {"completed": False, "error": f"unknown read scopes: {unknown}"}
        cred_ref = str(doc.get("credential_ref") or connector_id)
        saved = self.save_credentials(cred_ref, credentials)
        if not saved.get("saved"):
            return {
                "completed": False,
                "error": saved.get("error") or "vault write failed",
            }
        granted = scopes or sorted(valid_reads)
        doc["_scope_grants"] = list(granted)
        doc.pop("_disabled", None)
        save_connector(connector_id, doc)
        # Sensitive scope policy binding: write channels are not auto-opened,
        # per-use confirmation (recorded + queryable until the cowork #225
        # upstream lands; the #30 review queue backstops the execution side)
        state = self._bundles_state()
        policy = state.setdefault("_scope_policy", {})
        policy[connector_id] = {wid: "per_use" for wid in sorted(valid_writes)}
        # corresponding bundle item pending_auth -> enabled
        for bundle in state.values():
            for item in bundle.get("items", []):  # type: ignore[union-attr]
                if isinstance(item, dict) and item.get("type") == "connector" and item.get("ref") == connector_id:
                    item["state"] = "enabled"
        self._save_bundles_state(state)
        added = await self.enable_connector(connector_id)
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"connector {connector_id} OAuth completed: {len(granted)} read "
            f"channels granted, {len(valid_writes)} write channels bound to "
            "per-use confirmation",
        )
        # issue #43: per-scope grant events + sensitive policy binding events
        # (same trace_id)
        trace = audit_events.new_trace_id()
        for scope in sorted(granted):
            audit_events.emit(
                "hub.connector.oauth.grant",
                resource_type="connector",
                resource_id=connector_id,
                details={"scope": scope, "sensitive": False},
                risk_level="low",
                trace_id=trace,
            )
        for scope in sorted(valid_writes):
            audit_events.emit(
                "hub.connector.scope.policy",
                resource_type="connector",
                resource_id=connector_id,
                details={"scope": scope, "policy": "per_use", "sensitive": True},
                risk_level="high",
                trace_id=trace,
            )
        return {
            "completed": True,
            "connector_id": connector_id,
            "granted_scopes": list(granted),
            "sensitive_scopes_per_use": sorted(valid_writes),
            "capabilities": added.get("capabilities", []),
        }

    # Sensitive scope policy query (integration point for the cowork approval
    # system / permission engine).
    def scope_policy(self, connector_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        policy = (state.get("_scope_policy") or {}).get(connector_id, {})
        return {"connector_id": connector_id, "per_use_scopes": sorted(policy)}

    # ── update flow (issue #42: changelog first, new items opt-in) ──

    def bundle_update_preview(self, bundle_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"error": f"bundle {bundle_id!r} not installed"}
        entry = self._market_entry(bundle_id)
        installed_v = self._version_key(str(bundle.get("version") or "0"))
        available_v = self._version_key(entry.version)
        if available_v <= installed_v:
            return {
                "bundle_id": bundle_id,
                "update_available": False,
                "installed_version": bundle.get("version"),
                "available_version": entry.version,
            }
        known_refs = {i.get("ref") for i in bundle.get("items", [])}
        new_items = [dict(item) for item in self._entry_items(entry) if item.get("ref") not in known_refs]
        return {
            "bundle_id": bundle_id,
            "update_available": True,
            "installed_version": bundle.get("version"),
            "available_version": entry.version,
            "changelog": entry.description,
            "new_items": new_items,
        }

    # Apply the update: install only opt-in new items, bump the version.
    async def bundle_update_apply(self, bundle_id: str, opt_in: list[str]) -> dict[str, Any]:
        preview = self.bundle_update_preview(bundle_id)
        if not preview.get("update_available"):
            return {"updated": False, "reason": "no update available"}
        entry = self._market_entry(bundle_id)
        state = self._bundles_state()
        bundle = state[bundle_id]
        opted = set(opt_in or [])
        items_out: list[dict[str, Any]] = list(bundle.get("items", []))
        errors: list[str] = []
        for item in preview["new_items"]:
            ref = str(item.get("ref"))
            if ref not in opted:
                continue  # unticked new items are not auto-opened (opt-in contract)
            try:
                item_state = await self._install_bundle_item(str(item.get("type")), ref, item)
                items_out.append({"type": item.get("type"), "ref": ref, **item_state})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{item.get('type')}:{ref}: {exc}")
        bundle["items"] = items_out
        bundle["version"] = entry.version
        bundle["changelog"] = entry.description
        self._save_bundles_state(state)
        self.desk.log(
            bundle_id,
            "INFO",
            f"bundle {bundle_id} updated to v{entry.version}"
            f" (opt-in {len(opted)}/{len(preview['new_items'])} new items)",
        )
        audit_events.emit(
            "hub.bundle.update",
            resource_type="bundle",
            resource_id=bundle_id,
            details={"version": entry.version, "opt_in": list(opted)},
            risk_level="medium",
        )
        return {
            "updated": True,
            "bundle_id": bundle_id,
            "version": entry.version,
            "applied_items": list(opted),
            "errors": errors,
        }

    # ── uninstall + impact surface (issue #42: show in-use-by before removal) ──

    def uninstall_preview(self, bundle_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"error": f"bundle {bundle_id!r} not installed"}
        caps: list[str] = []
        for item in bundle.get("items", []):
            ref = str(item.get("ref"))
            if item.get("type") in ("skill", "subagent"):
                caps.append(f"node.{ref}")
                caps.append(ref)
            elif item.get("type") == "connector":
                doc = get_connector(ref) or {}
                caps.extend(f"connector.{ref}.{ch['id']}" for ch in read_channels(doc))
            elif item.get("type") == "connector_pack":
                caps.extend(self._pack_plugin_ids(ref))
        in_use_by = self._reverse_lookup(caps)
        return {
            "bundle_id": bundle_id,
            "capabilities_affected": caps,
            "in_use_by": in_use_by,
            "safe_to_remove": not in_use_by,
        }

    # Registry reverse lookup: which cowork nodes reference these capabilities
    # (defensive traversal).
    def _reverse_lookup(self, refs: list[str]) -> list[dict[str, Any]]:
        registry = getattr(self.desk, "node_registry", None)
        if registry is None or not refs:
            return []
        wanted = {r for r in refs if r}
        found: list[dict[str, Any]] = []
        try:
            nodes = registry.list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("extensions_console: node registry unreadable: %s", exc)
            return []
        for node in nodes:
            name = getattr(node, "name", None) or getattr(node, "__name__", "") or ""
            desc = getattr(node, "description", "") or ""
            hay = f"{name} {desc}"
            hits = sorted(w for w in wanted if w in hay)
            hit_list = sorted(hits)
            if hit_list:
                found.append({"node": str(name), "refs": hit_list})
        return found

    async def uninstall_bundle(self, bundle_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.pop(bundle_id, None)
        if bundle is None:
            return {"removed": False, "error": f"bundle {bundle_id!r} not installed"}
        removed: list[str] = []
        errors: list[str] = []
        for item in bundle.get("items", []):
            ref = str(item.get("ref"))
            try:
                if item.get("type") in ("skill", "subagent"):
                    if self._plugin_enabled(ref):
                        await self.lifecycle.disable(ref)
                    removed.append(ref)
                elif item.get("type") == "connector":
                    await self.disable_connector(ref)
                    delete_connector(ref)
                    removed.append(ref)
                elif item.get("type") == "connector_pack":
                    # Uninstall unregisters the bridges (no orphan manifests —
                    # issue #44 acceptance: consumers see no residue after hub
                    # uninstall); the inline creg holds no persistent state, so
                    # unregister directly
                    for pid in self._pack_plugin_ids(ref):
                        if self._plugin_enabled(pid):
                            await self.lifecycle.disable(pid)
                        self.registry.unregister(pid)
                    removed.append(ref)
                elif item.get("type") == "skill_package":
                    from fusion_plugins_ecosystem.marketplace import Marketplace

                    Marketplace(connector_registry=self._connector_registry()).uninstall(ref)
                    removed.append(ref)
            except Exception as exc:  # noqa: BLE001 — per-item errors do not abort the uninstall
                errors.append(f"{item.get('type')}:{ref}: {exc}")
        self._save_bundles_state(state)
        self.desk.log(
            bundle_id,
            "INFO",
            f"bundle {bundle_id} uninstalled ({len(removed)} items, {len(errors)} errors)",
        )
        audit_events.emit(
            "hub.bundle.uninstall",
            resource_type="bundle",
            resource_id=bundle_id,
            details={"items_removed": removed, "errors": errors},
            risk_level="medium",
        )
        return {
            "removed": True,
            "bundle_id": bundle_id,
            "items_removed": removed,
            "errors": errors,
        }

    # ── audit view + scope revoke + reverse lookup (issue #43 PC-3) ──

    def audit_records(
        self,
        resource_id: str = "",
        resource_type: str = "",
        action: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        # In-hub authorization audit view: who granted what and when (reads
        # the fusion-security trail).
        records = audit_events.query(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            trace_id=trace_id,
            limit=limit,
        )
        return {"records": records}

    async def revoke_scopes(self, connector_id: str, scopes: list[str]) -> dict[str, Any]:
        # Revoke granted read scopes: update _scope_grants, re-bridge, emit
        # audit events.
        # Revocations from the security workbench land back in the registry
        # here (effective on next load — the hub reads on-disk facts on every
        # enable/list, no in-memory second source of truth).
        doc = get_connector(connector_id)
        if not doc:
            return {"revoked": False, "error": "connector not found"}
        current = doc.get("_scope_grants")
        if not isinstance(current, list):
            current = [ch["id"] for ch in read_channels(doc)]
        target = [s for s in scopes if s in current]
        if not target:
            return {"revoked": False, "error": "no granted scopes in request"}
        remaining = [s for s in current if s not in target]
        if remaining:
            doc["_scope_grants"] = remaining
            save_connector(connector_id, doc)
            await self.enable_connector(connector_id)
        else:
            await self.disable_connector(connector_id)
            doc["_scope_grants"] = []
            save_connector(connector_id, doc)
        for scope in target:
            audit_events.emit(
                "hub.connector.oauth.revoke",
                resource_type="connector",
                resource_id=connector_id,
                details={"scope": scope},
                risk_level="medium",
            )
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"connector {connector_id} revoked read scopes: {target} (remaining {remaining})",
        )
        return {
            "revoked": True,
            "connector_id": connector_id,
            "revoked_scopes": target,
            "remaining_scopes": remaining,
        }

    # In-use-by: which cowork nodes reference this capability (standalone
    # entry point for the #42 reverse lookup).
    def capability_used_by(self, capability: str) -> dict[str, Any]:
        in_use_by = self._reverse_lookup([capability])
        return {"capability": capability, "in_use_by": in_use_by}

    # ── stable registry read contract (issue #44 PC-4: one registry, three surfaces) ──
    # Consumers: fusion-cowork#228 capability market, fusion-agent-studio#529
    # builder suggestions.
    # Contract version registry/1.0 — additive fields only, no semantic
    # changes; auth state is read-only, authorization actions go to the hub.

    REGISTRY_CONTRACT = "registry/1.0"
    HUB_URL = "/shell/fusion-plugins-ecosystem/"

    @staticmethod
    def _registry_kind(manifest: Any) -> str:
        caps = {c.value for c in manifest.capabilities}
        if "claude_skill" in caps:
            return "skill"
        if "subagent" in caps:
            return "subagent"
        return "plugin"

    # Granted read scope list; None = fully granted (#30 direct-install path,
    # no _scope_grants).
    def _connector_grants(self, connector_id: str) -> list[str] | None:
        doc = get_connector(connector_id)
        if not doc:
            return None
        grants = doc.get("_scope_grants")
        if not isinstance(grants, list):
            return None
        return grants

    # registry/1.0 full capability listing: single source of truth, consumers
    # must not fork caches.
    # Connectors expand by document read channels (not just bridged manifests)
    # — revoked/pending channels surface via auth.state so consumers see the
    # full authorization surface, not only active bridges.
    def registry_capabilities(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for entry in connector_report():
            cid = entry["id"]
            doc = get_connector(cid) or {}
            grants = self._connector_grants(cid)
            for ch in read_channels(doc):
                bridged = self.registry.get(f"connector__{cid}__{ch['id']}")
                enabled = bridged is not None and self._plugin_enabled(bridged.id)
                granted = grants is None or ch["id"] in grants
                if not granted:
                    state = "revoked"
                elif enabled:
                    state = "granted"
                else:
                    state = "pending"
                items.append(
                    {
                        "id": f"connector.{cid}.{ch['id']}",
                        "kind": "connector",
                        "name": f"{cid} · {ch['title']}",
                        "version": str(doc.get("version") or "1.0.0"),
                        "description": ch["description"],
                        "enabled": enabled,
                        "auth": {
                            "state": state,
                            "granted": granted,
                            "granted_scopes": grants if grants is not None else [],
                            "all_scopes_granted": grants is None,
                            "hub_url": self.HUB_URL,
                        },
                    }
                )
        # Connector packs (issue #27 ConnectorRegistry bridges) have no rest
        # doc — listed by bridge manifests, authorization surface = full
        # (pack tools have no per-scope model, aligned with the #30
        # direct-install path)
        doc_cids = {entry["id"] for entry in connector_report()}
        for m in self.registry.list():
            if m.id.startswith("connector__"):
                cid = m.id.split("__", 2)[1]
                if cid in doc_cids:
                    continue  # rest connectors are covered by document read-channel expansion
                _, cid, op = m.id.split("__", 2)
                items.append(
                    {
                        "id": f"connector.{cid}.{op}",
                        "kind": "connector",
                        "name": m.name,
                        "version": m.version,
                        "description": m.description or "",
                        "enabled": self._plugin_enabled(m.id),
                        "auth": {
                            "state": "granted",
                            "granted": True,
                            "granted_scopes": [],
                            "all_scopes_granted": True,
                            "hub_url": self.HUB_URL,
                        },
                    }
                )
                continue
            if m.id.startswith("mcp__"):
                _, sid, tool = m.id.split("__", 2)
                items.append(
                    {
                        "id": f"mcp.{sid}.{tool}",
                        "kind": "mcp_tool",
                        "name": m.name,
                        "version": m.version,
                        "description": m.description or "",
                        "enabled": self._plugin_enabled(m.id),
                        "auth": {
                            "state": "none",
                            "granted": True,
                            "granted_scopes": [],
                            "all_scopes_granted": True,
                            "hub_url": self.HUB_URL,
                        },
                    }
                )
            else:
                items.append(
                    {
                        "id": f"node.{m.id}",
                        "kind": self._registry_kind(m),
                        "name": m.name,
                        "version": m.version,
                        "description": m.description or "",
                        "enabled": self._plugin_enabled(m.id),
                        "auth": {
                            "state": "none",
                            "granted": True,
                            "granted_scopes": [],
                            "all_scopes_granted": True,
                            "hub_url": self.HUB_URL,
                        },
                    }
                )
        logger.info("extensions_console: registry/1.0 listing has %d items", len(items))
        return {"contract": self.REGISTRY_CONTRACT, "capabilities": items}

    # registry/1.0 single-capability detail: listing fields + per-use policy
    # + in-use-by.
    def registry_capability(self, capability: str) -> dict[str, Any]:
        listing = self.registry_capabilities()["capabilities"]
        item = next((c for c in listing if c["id"] == capability), None)
        if item is None:
            return {"error": f"capability {capability!r} not found"}
        out = dict(item)
        if item["kind"] == "connector":
            cid = item["id"].split(".", 2)[1]
            out["per_use_scopes"] = self.scope_policy(cid).get("per_use_scopes") or []
        out["in_use_by"] = self._reverse_lookup([item["id"]])
        return out

    # ── connectors ──

    def list_connectors(self) -> dict[str, Any]:
        spine: dict[str, list[str]] = {}
        for c in self.list_capabilities():
            if c["source"] == "connector":
                cid = c["route"]["connector"]
                suffix = "" if c["status"] == "active" else " (disabled)"
                spine.setdefault(cid, []).append(c["id"] + suffix)
        rows = []
        for entry in connector_report():
            cid = entry["id"]
            doc = get_connector(cid) or {}
            rows.append(
                {
                    **entry,
                    "kind": doc.get("kind", ""),
                    "write_channels_review": write_channels(doc),
                    "spine_capabilities": spine.get(cid, []),
                }
            )
        return {"connectors": rows, "registry": [r["id"] for r in rows]}

    # Full static validation + persist to disk (the install record is the
    # file). Runtime registration happens only on enable.
    def install_connector(self, doc: dict[str, Any]) -> dict[str, Any]:
        problems = validate_connector(doc)
        if problems:
            return {"installed": False, "problems": problems}
        cid = str(doc["id"])
        doc.pop("_disabled", None)
        save_connector(cid, doc)
        reads = read_channels(doc)
        writes = write_channels(doc)
        self.desk.log(
            f"connector__{cid}",
            "INFO",
            f"connector {cid} installed ({len(reads)} read channels, {len(writes)} write channels pending review)",
        )
        logger.info(
            "extensions_console: connector %s installed by %s (%d reads, %d writes not auto-listed)",
            cid,
            self.installed_by,
            len(reads),
            len(writes),
        )
        return {
            "installed": True,
            "connector_id": cid,
            "read_channels": [c["id"] for c in reads],
            "write_channels_review": writes,
        }

    def save_credentials(self, credential_ref: str, credentials: dict[str, Any]) -> dict[str, Any]:
        if not credential_ref or not isinstance(credentials, dict):
            return {"saved": False, "error": "missing credential_ref or credentials"}
        stored = json.dumps(credentials, ensure_ascii=False)
        # Prefer Desk encrypted storage (cowork config_center, Fernet);
        # round-trip read verification — a standalone console (no
        # config_center) silently drops writes, so fall back to the
        # console-local encrypted vault (same _encrypt_key derived key).
        if hasattr(self.desk, "set_api_key"):
            self.desk.set_api_key(credential_ref, stored)
            if hasattr(self.desk, "get_api_key") and self.desk.get_api_key(credential_ref) == stored:
                logger.info(
                    "extensions_console: credentials %s written to Desk encrypted storage",
                    credential_ref,
                )
                return {"saved": True, "credential_ref": credential_ref}
        try:
            from fusion_plugins_ecosystem.desk_runtime import _encrypt_key
        except ImportError:
            return {
                "saved": False,
                "error": "vault unavailable (no Desk config center, no local encryption)",
            }
        vault = _local_vault_read()
        vault[credential_ref] = _encrypt_key(stored)
        _local_vault_write(vault)
        logger.info(
            "extensions_console: credentials %s written to console-local encrypted vault",
            credential_ref,
        )
        return {"saved": True, "credential_ref": credential_ref}

    async def enable_connector(self, connector_id: str) -> dict[str, Any]:
        doc = get_connector(connector_id)
        if not doc:
            return {"enabled": False, "error": "connector not found"}
        doc.pop("_disabled", None)
        save_connector(connector_id, doc)
        added = await self._bridge_connector(doc)
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"connector {connector_id} enabled, auto-listing {len(added)} read capabilities",
        )
        return {"enabled": True, "connector_id": connector_id, "capabilities": added}

    async def disable_connector(self, connector_id: str) -> dict[str, Any]:
        doc = get_connector(connector_id)
        if doc:
            doc["_disabled"] = True
            save_connector(connector_id, doc)
        affected = await self._unbridge_connector(connector_id)
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"connector {connector_id} disabled, {len(affected)} capabilities flipped",
        )
        return {
            "disabled": True,
            "connector_id": connector_id,
            "capabilities_disabled": affected,
        }

    # Delete a connector: unbridge + delete doc + clear scope policy
    # (issue #43 completes the lifecycle).
    async def remove_connector(self, connector_id: str) -> dict[str, Any]:
        doc = get_connector(connector_id)
        if not doc:
            return {"removed": False, "error": "connector not found"}
        affected = await self._unbridge_connector(connector_id)
        delete_connector(connector_id)
        state = self._bundles_state()
        policy = state.setdefault("_scope_policy", {})
        dropped_policy = policy.pop(connector_id, None)
        self._save_bundles_state(state)
        audit_events.emit(
            "hub.connector.remove",
            resource_type="connector",
            resource_id=connector_id,
            details={
                "capabilities_removed": len(affected),
                "policy_cleared": bool(dropped_policy),
            },
            risk_level="medium",
        )
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"connector {connector_id} removed, {len(affected)} capabilities detached",
        )
        return {
            "removed": True,
            "connector_id": connector_id,
            "capabilities_removed": affected,
        }

    # Conformance harness: static validation + live check of the first read
    # op using vault credentials.
    async def harness(self, connector_id: str) -> dict[str, Any]:
        doc = get_connector(connector_id)
        if not doc:
            return {
                "connector_id": connector_id,
                "problems": ["connector not found"],
                "conformant": False,
            }
        problems = validate_connector(doc)
        reads = read_channels(doc)
        if not reads:
            problems.append("no read operations to verify — harness needs >=1 GET op")
        elif not problems:
            creds = _vault_credentials(self.desk, str(doc.get("credential_ref") or ""))
            result = await execute_operation(doc, reads[0]["id"], credentials=creds)
            if not result.get("ok"):
                problems.append(
                    f"{reads[0]['id']} live check failed: {result.get('error_code')}: {result.get('reason')}"
                )
        return {
            "connector_id": connector_id,
            "problems": problems,
            "conformant": not problems,
        }

    # ── bridging (connector read channels -> capability plugins) ──

    async def _bridge_connector(self, doc: dict[str, Any]) -> list[str]:
        from fusion_plugins_ecosystem.registry import (
            PluginCapability,
            PluginCategory,
            PluginManifest,
        )
        from fusion_plugins_ecosystem.schema import MCPAnnotations, SandboxMode

        cid = str(doc.get("id"))
        await self._unbridge_connector(cid)
        added = []
        # issue #42: OAuth per-scope grants — bridge only granted read
        # channels (doc._scope_grants absent = full grant, compatible with the
        # #30 direct-install/direct-enable path)
        granted = doc.get("_scope_grants")
        for ch in read_channels(doc):
            if isinstance(granted, list) and ch["id"] not in granted:
                logger.info(
                    "extensions_console: connector %s read channel %s not granted, skipping bridge",
                    cid,
                    ch["id"],
                )
                continue
            op = (doc.get("operations") or {}).get(ch["id"]) or {}
            plugin_id = f"connector__{cid}__{ch['id']}"
            self.registry.register(
                PluginManifest(
                    id=plugin_id,
                    name=f"{cid} · {ch['id']}",
                    version="1.0.0",
                    category=PluginCategory.CONNECTOR,
                    description=ch["description"],
                    capabilities=(PluginCapability.MCP_TOOL,),
                    entry_point=_RestOpBound(cid, ch["id"], op),
                    default_mounted=False,
                    sandbox_mode=SandboxMode.INLINE,
                    mcp_annotations=MCPAnnotations(readOnlyHint=True, openWorldHint=True),
                )
            )
            added.append(f"connector.{cid}.{ch['id']}")
        await self._enable_all(f"connector__{cid}__")
        logger.info("extensions_console: connector %s bridged %d read channels", cid, len(added))
        return added

    # Bridge = expose: read channels / ticked tools are set ENABLED right
    # after registration (auto-list contract).
    async def _enable_all(self, prefix: str) -> None:
        if self.lifecycle is None:
            return
        for m in list(self.registry.list()):
            if m.id.startswith(prefix):
                try:
                    await self.lifecycle.enable(m.id)
                except Exception as exc:  # noqa: BLE001 — per-item errors do not abort the batch
                    logger.warning("extensions_console: failed to enable %s: %s", m.id, exc)

    async def _unbridge_connector(self, connector_id: str) -> list[str]:
        removed = []
        prefix = f"connector__{connector_id}__"
        for manifest in list(self.registry.list()):
            if manifest.id.startswith(prefix):
                if self.lifecycle is not None:
                    try:
                        await self.lifecycle.disable(manifest.id)
                    except Exception:  # noqa: BLE001 — unregister even if disable fails
                        pass
                self.registry.unregister(manifest.id)
                removed.append(manifest.id)
        return removed

    # ── plugins (tick/untick reconciliation + declarative install) ──

    def _base_of(self, plugin_id: str) -> str:
        for prefix in ("connector__", "mcp__"):
            if plugin_id.startswith(prefix):
                parts = plugin_id[len(prefix) :].split("__", 1)
                if len(parts) == 2:
                    return parts[0]
        return plugin_id

    def _node_of(self, plugin_id: str) -> str | None:
        for prefix in ("connector__", "mcp__"):
            if plugin_id.startswith(prefix):
                parts = plugin_id[len(prefix) :].split("__", 1)
                if len(parts) == 2:
                    return parts[1]
        return None

    def _plugin_enabled(self, plugin_id: str) -> bool:
        if self.lifecycle is None:
            return False
        for st in self.lifecycle.list_states():
            if st.get("plugin_id") == plugin_id:
                return st.get("state") == "enabled"
        return False

    def list_plugins(self) -> dict[str, Any]:
        groups: dict[str, dict[str, Any]] = {}
        for manifest in self.registry.list():
            base = self._base_of(manifest.id)
            node = self._node_of(manifest.id)
            g = groups.setdefault(
                base,
                {
                    "name": base,
                    "version": manifest.version,
                    "sandbox": manifest.sandbox_mode.value == "process",
                    "signature": None,
                    "nodes": [],
                    "ticked": [],
                },
            )
            g["nodes"].append(node if node is not None else manifest.id)
            if self._plugin_enabled(manifest.id):
                g["ticked"].append(node if node is not None else manifest.id)
        for g in groups.values():
            g["nodes"].sort()
            g["ticked"].sort()
        return {
            "plugins": sorted(groups.values(), key=lambda g: g["name"]),
            "require_signing": False,
        }

    # Apply ticks: reconcile the ticked set — ticked get enabled, unticked
    # get disabled.
    async def tick_nodes(self, plugin_base: str, nodes: list[str]) -> dict[str, Any]:
        target = [m for m in self.registry.list() if self._base_of(m.id) == plugin_base]
        if not target:
            return {"ticked": [], "errors": [f"plugin '{plugin_base}' not found"]}
        wanted = set(nodes)
        ticked, errors = [], []
        for m in target:
            node = self._node_of(m.id) or m.id
            if self.lifecycle is None:
                errors.append("lifecycle unavailable — cannot tick")
                continue
            try:
                already = self._plugin_enabled(m.id)
                if node in wanted:
                    if not already:
                        await self.lifecycle.enable(m.id)
                    ticked.append(node)
                elif already:
                    await self.lifecycle.disable(m.id)
            except Exception as exc:  # noqa: BLE001 — per-node errors do not abort the batch
                errors.append(f"{node}: {exc}")
        self._persist_ticks(plugin_base, sorted(wanted))
        return {"ticked": ticked, "errors": errors}

    def _persist_ticks(self, plugin_base: str, nodes: list[str]) -> None:
        ticks = self._read_json(_TICKS_KEY, {})
        if nodes:
            ticks[plugin_base] = sorted(set(nodes))
        else:
            ticks.pop(plugin_base, None)
        self._write_json(_TICKS_KEY, ticks)

    async def remove_plugin(self, plugin_base: str) -> dict[str, Any]:
        removed = []
        for m in list(self.registry.list()):
            if self._base_of(m.id) == plugin_base:
                if self.lifecycle is not None:
                    try:
                        await self.lifecycle.disable(m.id)
                    except Exception:  # noqa: BLE001 — unregister even if disable fails
                        pass
                self.registry.unregister(m.id)
                removed.append(m.id)
        self._persist_ticks(plugin_base, [])
        custom = _extensions_dir() / _PLUGINS_KEY / f"{plugin_base}.json"
        if custom.exists():
            custom.unlink()
        self.desk.log(
            plugin_base,
            "INFO",
            f"plugin {plugin_base} removed ({len(removed)} capabilities)",
        )
        return {"removed": bool(removed), "capabilities_removed": removed}

    # Install a declarative plugin pack: dir or zip with a plugin.json
    # manifest (no arbitrary code on disk).
    def install_plugin(self, source: str) -> dict[str, Any]:
        src = Path(source).expanduser()
        manifest_file = src / "plugin.json" if src.is_dir() else None
        tmp_dir: Path | None = None
        if src.suffix == ".zip" and src.exists():
            tmp_dir = Path(_extensions_dir()) / "_tmp_install"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src) as zf:
                zf.extractall(tmp_dir)
            candidates = list(tmp_dir.rglob("plugin.json"))
            manifest_file = candidates[0] if candidates else None
        if manifest_file is None or not manifest_file.exists():
            return {
                "installed": False,
                "error": "source must be a dir or zip containing plugin.json",
            }
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest = _manifest_from_decl(data)
        except (ValueError, KeyError, TypeError) as exc:
            return {"installed": False, "error": f"invalid plugin.json: {exc}"}
        self.registry.register(manifest)
        if tmp_dir is None:
            store = _extensions_dir() / _PLUGINS_KEY
            store.mkdir(parents=True, exist_ok=True)
            (store / f"{manifest.id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        nodes = [self._node_of(manifest.id) or manifest.id]
        self._persist_ticks(manifest.id, [])
        self.desk.log(
            manifest.id,
            "INFO",
            f"declarative plugin {manifest.id} installed, candidate nodes: {nodes} (exposed after tick)",
        )
        return {
            "installed": True,
            "plugin": manifest.id,
            "candidate_nodes": nodes,
            "note": "candidates only — tick to expose",
        }

    # ── remote MCP intake ──

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        ticks: dict[str, list[str]] = data.get("ticks") or {}
        out = []
        for s in data.get("servers") or []:
            entry = dict(s)
            entry["ticked"] = sorted(ticks.get(s.get("id", ""), []))
            out.append(entry)
        return out

    def register_mcp_server(self, server_id: str, url: str, credential_ref: str = "") -> dict[str, Any]:
        # Persist only after the probe (initialize + tools/list) passes —
        # prove the endpoint speaks MCP first.
        server_id = str(server_id or "").strip()
        url = str(url or "").strip()
        if not server_id or "/" in server_id or server_id.startswith("."):
            return {"registered": False, "error": "missing/invalid server id"}
        if not (url.startswith("http://") or url.startswith("https://")):
            return {
                "registered": False,
                "error": "url must be http(s) — no stdio from the console",
            }
        try:
            auth_headers = _vault_headers(self.desk, credential_ref)
        except RuntimeError as exc:
            return {"registered": False, "error": str(exc)}
        try:
            _mcp_rpc(
                url,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "fusion-plugins-ecosystem-console",
                        "version": "1.0",
                    },
                },
                headers=auth_headers,
            )
            tools = _mcp_rpc(url, "tools/list", headers=auth_headers).get("tools") or []
        except Exception as exc:  # noqa: BLE001 — honest probe envelope
            return {
                "registered": False,
                "error": f"probe failed: {type(exc).__name__}: {exc}"[:300],
            }
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        servers = [s for s in data.get("servers") or [] if s.get("id") != server_id]
        servers.append(
            {
                "id": server_id,
                "url": url,
                "credential_ref": credential_ref,
                "tools": [
                    {
                        "name": t.get("name"),
                        "description": str(t.get("description") or ""),
                    }
                    for t in tools
                ],
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        data["servers"] = servers
        self._write_json(_MCP_KEY, data)
        self._persist_mcp_ticks(server_id, [])
        return {
            "registered": True,
            "server": server_id,
            "candidate_tools": [t["name"] for t in servers[-1]["tools"]],
        }

    def remove_mcp_server(self, server_id: str) -> dict[str, Any]:
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        data["servers"] = [s for s in data.get("servers") or [] if s.get("id") != server_id]
        ticks = data.get("ticks") or {}
        ticks.pop(server_id, None)
        data["ticks"] = ticks
        self._write_json(_MCP_KEY, data)
        removed = []
        prefix = f"mcp__{server_id}__"
        for m in list(self.registry.list()):
            if m.id.startswith(prefix):
                self.registry.unregister(m.id)
                removed.append(m.id)
        return {"removed": True, "server": server_id, "capabilities_removed": removed}

    # Tick tools -> bridge into mcp__<server>__<tool> capability plugins
    # (re-tick performs a full reconciliation).
    async def tick_mcp_tools(self, server_id: str, tools: list[str]) -> dict[str, Any]:
        server = next((s for s in self.list_mcp_servers() if s.get("id") == server_id), None)
        if not server:
            return {"ticked": [], "errors": [f"server '{server_id}' unknown"]}
        known = {t.get("name"): t for t in server.get("tools", [])}
        ticked, errors = [], []
        for name in tools:
            if name not in known:
                errors.append(f"'{name}' is not a tool of '{server_id}'")
        valid = [t for t in tools if t in known]
        self._persist_mcp_ticks(server_id, sorted(set(valid)))
        bridged = await self._bridge_mcp_tools(server, valid)
        ticked.extend(bridged)
        if errors:
            logger.warning(
                "extensions_console: MCP tick partially failed %s: %s",
                server_id,
                errors,
            )
        return {"ticked": ticked, "errors": errors}

    # MCP ticks live in the same file as servers (mcp_servers.json) — single
    # source of truth.
    def _persist_mcp_ticks(self, server_id: str, tools: list[str]) -> None:
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        ticks = data.get("ticks") or {}
        if tools:
            ticks[server_id] = sorted(set(tools))
        else:
            ticks.pop(server_id, None)
        data["ticks"] = ticks
        self._write_json(_MCP_KEY, data)

    async def _bridge_mcp_tools(self, server: dict[str, Any], tools: list[str]) -> list[str]:
        from fusion_plugins_ecosystem.connector import _schema_to_params
        from fusion_plugins_ecosystem.registry import (
            PluginCapability,
            PluginCategory,
            PluginManifest,
        )
        from fusion_plugins_ecosystem.schema import MCPAnnotations, SandboxMode

        sid = server["id"]
        prefix = f"mcp__{sid}__"
        for m in list(self.registry.list()):
            if m.id.startswith(prefix):
                if self.lifecycle is not None:
                    try:
                        await self.lifecycle.disable(m.id)
                    except Exception:  # noqa: BLE001 — unregister even if disable fails
                        pass
                self.registry.unregister(m.id)
        bridged = []
        by_name = {t.get("name"): t for t in server.get("tools", [])}
        for name in tools:
            spec = by_name.get(name) or {}
            read_only = bool((spec.get("annotations") or {}).get("readOnlyHint", False))
            plugin_id = f"{prefix}{name}"
            self.registry.register(
                PluginManifest(
                    id=plugin_id,
                    name=f"{sid} · {name}",
                    version="1.0.0",
                    category=PluginCategory.CONNECTOR,
                    description=spec.get("description", ""),
                    capabilities=(PluginCapability.MCP_TOOL,),
                    params=_schema_to_params(spec.get("input_schema") or {}),
                    entry_point=_McpToolBound(self, sid, name),
                    default_mounted=False,
                    sandbox_mode=SandboxMode.INLINE,
                    mcp_annotations=MCPAnnotations(readOnlyHint=read_only, openWorldHint=True),
                )
            )
            bridged.append(f"mcp.{sid}.{name}")
        await self._enable_all(prefix)
        logger.info("extensions_console: MCP server %s bridged %d tools", sid, len(bridged))
        return bridged

    async def call_mcp_tool(self, server_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # Gate-side execution: tools/call dispatched to the registered URL,
        # auth headers resolved server-side.
        server = next((s for s in self.list_mcp_servers() if s.get("id") == server_id), None)
        if not server:
            return {"ok": False, "error": f"MCP server '{server_id}' not registered"}
        try:
            auth_headers = _vault_headers(self.desk, str(server.get("credential_ref") or ""))
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            result = await asyncio.to_thread(
                _mcp_rpc,
                server["url"],
                "tools/call",
                {"name": tool, "arguments": args or {}},
                headers=auth_headers,
            )
        except Exception as exc:  # noqa: BLE001 — honest envelope
            return {
                "ok": False,
                "error": f"tools/call failed: {type(exc).__name__}: {exc}"[:300],
            }
        return {"ok": True, "result": result}

    # ── startup resync (rebuild runtime bridges from on-disk facts) ──

    async def resync(self) -> dict[str, Any]:
        relisted: list[str] = []
        for entry in connector_report():
            cid = entry["id"]
            doc = get_connector(cid) or {}
            if not doc or doc.get("_disabled"):
                continue
            relisted.extend(await self._bridge_connector(doc))
        for server in self.list_mcp_servers():
            sid = server.get("id", "")
            known = {x.get("name") for x in server.get("tools", [])}
            valid = [t for t in server.get("ticked", []) if t in known]
            if valid:
                self._persist_mcp_ticks(sid, valid)
                relisted.extend(await self._bridge_mcp_tools(server, valid))
        custom_dir = _extensions_dir() / _PLUGINS_KEY
        if custom_dir.exists():
            for path in sorted(custom_dir.glob("*.json")):
                try:
                    self.registry.register(_manifest_from_decl(json.loads(path.read_text(encoding="utf-8"))))
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "extensions_console: skipping corrupt plugin manifest %s: %s",
                        path.name,
                        exc,
                    )
        # issue #44: rebuild enabled items of installed bundles across
        # restarts (connector pack bridges / skill enables are runtime state;
        # without rebuild they "disappear" on the consumer's next pull)
        for bundle in self.list_bundles()["bundles"]:
            for item in bundle.get("items", []):
                if item.get("state") != "enabled":
                    continue
                itype = str(item.get("type"))
                ref = str(item.get("ref"))
                try:
                    if itype == "connector_pack":
                        if self._pack_plugin_ids(ref):
                            continue  # bridges already exist (repeated resync) — no reinstall
                        out = await self._install_bundle_item(itype, ref, {})
                        relisted.extend(out.get("plugin_ids", []))
                    elif itype in ("skill", "subagent") and self.registry.get(ref):
                        if not self._plugin_enabled(ref):
                            await self.lifecycle.enable(ref)
                        relisted.append(ref)
                except Exception as exc:  # noqa: BLE001 — a single item failure does not break resync
                    logger.warning(
                        "extensions_console: resync failed to rebuild bundle item %s:%s: %s",
                        bundle.get("id"),
                        ref,
                        exc,
                    )
        logger.info("extensions_console: resync rebuilt %d capabilities", len(relisted))
        return {"relisted": relisted}
