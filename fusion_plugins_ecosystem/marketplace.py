# Skill/connector pack marketplace.
# Maps to issue #27 "Skill/plugin marketplace":
# - Pulls the installable package catalog from a JSON index (local path or
#   HTTPS URL), with the builtin first-party catalog as fallback
# - connector_pack: parsed into a ConnectorPack and installed into
#   ConnectorRegistry (tools bridged as cowork nodes)
# - skill_package: cowork markdown skill package, installed to a local dir
# - Enterprise private marketplace: point connector_marketplace_index at a
#   private index URL
# Security constraint: remote index/sources allow HTTPS only; git sources
# are shallow-cloned into the install dir.

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any

from fusion_plugins_ecosystem.config import DATA_DIR
from fusion_plugins_ecosystem.connector import ConnectorPack, ConnectorRegistry

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_ROOT = str(DATA_DIR / "marketplace")  # issue #38: single-point data dir resolution
PACKAGE_KINDS = ("connector_pack", "skill_package", "bundle")
BUNDLE_ITEM_TYPES = (
    "skill",
    "subagent",
    "connector",
    "connector_pack",
    "skill_package",
)


@dataclass(frozen=True)
# Marketplace entry (one row of the index).
class MarketplaceEntry:
    id: str
    kind: str  # connector_pack | skill_package | bundle (issue #42)
    name: str
    version: str
    description: str = ""
    source: str | None = None  # git URL / local path (None for builtin packs)
    pack: dict[str, Any] | None = None  # inline connector_pack definition (builtin)
    items: list[dict[str, Any]] | None = None  # bundle entries (issue #42 PC-2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "items": self.items,
        }


class Marketplace:
    # Connector pack / skill package marketplace: index loading +
    # install/uninstall/installed listing.
    def __init__(
        self,
        connector_registry: ConnectorRegistry,
        install_root: str | None = None,
        marketplace_index: str | None = None,
    ) -> None:
        self.connector_registry = connector_registry
        self.install_root = os.path.expanduser(install_root or DEFAULT_INSTALL_ROOT)
        self.marketplace_index = marketplace_index

    # ── Index ──

    def builtin_catalog(self) -> list[MarketplaceEntry]:
        # Builtin first-party connector pack catalog (installable without an index).
        from fusion_plugins_ecosystem.connector_packs import list_builtin_packs

        return [
            MarketplaceEntry(
                id=p.id,
                kind="connector_pack",
                name=p.name,
                version=p.version,
                description=p.description,
                pack=p.to_dict(),
            )
            for p in list_builtin_packs()
        ]

    def load_index(self, source: str | None = None) -> tuple[list[MarketplaceEntry], list[str]]:
        # Load the marketplace index, returning (valid entries, warnings).
        # source: local JSON path or HTTPS URL; None uses the constructor
        # arg; when both are empty only the builtin catalog is returned.
        # Invalid entries are skipped with a warning so one bad row cannot
        # take down the whole index.
        entries: list[MarketplaceEntry] = list(self.builtin_catalog())
        warnings: list[str] = []
        src = source or self.marketplace_index
        if not src:
            return entries, warnings
        try:
            raw = self._fetch_json(src)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            warnings.append(f"marketplace: index load failed {src!r}: {exc}")
            logger.warning("marketplace: %s", warnings[-1])
            return entries, warnings
        for item in raw.get("packages", []):
            if not isinstance(item, dict):
                warnings.append(f"marketplace: skipping non-object entry {item!r}")
                continue
            entry, err = self._validate_entry(item)
            if err:
                warnings.append(err)
                continue
            entries.append(entry)
        # Index entries override builtin entries with the same ID (index
        # wins), keeping catalog IDs unique
        deduped: dict[str, MarketplaceEntry] = {}
        for e in entries:
            deduped[e.id] = e
        final = list(deduped.values())
        logger.info("marketplace: index %s loaded %d entries", src, len(final))
        return final, warnings

    @staticmethod
    def _fetch_json(src: str) -> dict[str, Any]:
        if src.startswith("https://"):
            with urllib.request.urlopen(src, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        if src.startswith("http://"):
            raise ValueError("marketplace: HTTPS-only index allowed (security constraint)")
        with open(os.path.expanduser(src), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _validate_entry(
        item: dict[str, Any],
    ) -> tuple[MarketplaceEntry | None, str | None]:
        pid = item.get("id")
        kind = item.get("kind")
        if not pid or not isinstance(pid, str):
            return None, "marketplace: entry missing id"
        if kind not in PACKAGE_KINDS:
            return None, f"marketplace: {pid} invalid kind={kind!r}"
        entry = MarketplaceEntry(
            id=pid,
            kind=kind,
            name=item.get("name", pid),
            version=str(item.get("version", "0.0.0")),
            description=item.get("description", ""),
            source=item.get("source"),
            pack=item.get("pack"),
            items=item.get("items"),
        )
        if entry.kind == "connector_pack" and entry.pack is None and not entry.source:
            return None, f"marketplace: {pid} has neither inline pack nor source"
        if entry.kind == "bundle":
            items = entry.items
            if not isinstance(items, list) or not items:
                return None, f"marketplace: {pid} bundle missing items"
            for it in items:
                if not isinstance(it, dict) or it.get("type") not in BUNDLE_ITEM_TYPES:
                    return None, f"marketplace: {pid} invalid bundle item {it!r}"
        return entry, None

    # ── Install / uninstall ──

    def install(self, entry_id: str, source: str | None = None) -> dict[str, Any]:
        # Install a marketplace package, returning the result (kind plus
        # artifact path / bridged plugin IDs).
        entries, _ = self.load_index(source)
        entry = next((e for e in entries if e.id == entry_id), None)
        if entry is None:
            raise KeyError(f"marketplace: no entry {entry_id!r} in index")
        if entry.kind == "connector_pack":
            return self._install_connector_pack(entry)
        if entry.kind == "bundle":
            raise ValueError(
                f"marketplace: bundle {entry.id!r} must be installed via "
                "ExtensionsConsole install_bundle (cross-registry orchestration, issue #42)"
            )
        return self._install_skill_package(entry)

    def _install_connector_pack(self, entry: MarketplaceEntry) -> dict[str, Any]:
        pack_dict = entry.pack
        if pack_dict is None:
            pack_dir = self._materialize_source(entry)
            pack_file = os.path.join(pack_dir, "pack.json")
            if not os.path.exists(pack_file):
                raise ValueError(f"marketplace: {entry.id} source missing pack.json")
            with open(pack_file, encoding="utf-8") as f:
                pack_dict = json.load(f)
            pack_dict.setdefault("id", entry.id)
        pack = ConnectorPack.from_dict(pack_dict)
        plugin_ids = self.connector_registry.install_pack(pack)
        return {"kind": "connector_pack", "id": pack.id, "plugin_ids": plugin_ids}

    def _install_skill_package(self, entry: MarketplaceEntry) -> dict[str, Any]:
        if not entry.source:
            raise ValueError(f"marketplace: skill package {entry.id} missing source")
        dest = os.path.join(self.install_root, entry.id)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        src = self._materialize_source(entry)
        shutil.copytree(src, dest)
        self.desk_log(f"skill package {entry.id} installed to {dest}")
        return {"kind": "skill_package", "id": entry.id, "path": dest}

    def _materialize_source(self, entry: MarketplaceEntry) -> str:
        # Materialize entry.source into a local dir: git URLs are
        # shallow-cloned; local paths are returned directly.
        src = entry.source
        if not src:
            raise ValueError(f"marketplace: {entry.id} missing source")
        if src.startswith("https://") and src.endswith(".git"):
            dest = os.path.join(self.install_root, "_src", entry.id)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", src, dest],
                check=True,
                timeout=120,
                capture_output=True,
            )
            return dest
        if src.startswith("http://") or src.startswith("ftp://"):
            raise ValueError("marketplace: HTTPS/local sources only (security constraint)")
        path = os.path.expanduser(src)
        if not os.path.isdir(path):
            raise ValueError(f"marketplace: {entry.id} local source does not exist: {src}")
        return path

    def uninstall(self, entry_id: str) -> None:
        # Uninstall a marketplace package (connector packs unregister their
        # bridged plugins; skill packages delete the install dir).
        self.connector_registry.uninstall_pack(entry_id)
        dest = os.path.join(self.install_root, entry_id)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
            self.desk_log(f"{entry_id} uninstalled")

    def list_installed(self) -> list[dict[str, Any]]:
        # List installed packages: connector packs + skill package dirs.
        installed: list[dict[str, Any]] = []
        for pack in self.connector_registry.list_packs():
            installed.append({"kind": "connector_pack", "id": pack.id, "version": pack.version})
        if os.path.isdir(self.install_root):
            for name in sorted(os.listdir(self.install_root)):
                path = os.path.join(self.install_root, name)
                if name.startswith("_") or not os.path.isdir(path):
                    continue
                installed.append({"kind": "skill_package", "id": name, "path": path})
        return installed

    def desk_log(self, message: str) -> None:
        # Record a marketplace operation via Desk (falls back to module
        # logging when no desk is available).
        desk = getattr(self.connector_registry, "desk", None)
        if desk is not None:
            desk.log("marketplace", "INFO", message)
        else:
            logger.info("marketplace: %s", message)
