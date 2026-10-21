"""技能/连接器包市场（marketplace）。

对应 issue #27「Skill/plugin marketplace」：
- 从 JSON 索引（本地路径或 HTTPS URL）拉取可安装包目录，内置第一方目录兜底
- connector_pack：解析为 ConnectorPack 安装进 ConnectorRegistry（工具桥接为
  cowork 节点）
- skill_package：cowork markdown skill 包，安装到本地目录
- 企业私有市场：把 connector_marketplace_index 指向私有索引 URL 即可

安全约束：远程索引/来源仅允许 HTTPS；git 来源浅克隆到安装目录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Any

from fusion_plugins_ecosystem.connector import ConnectorPack, ConnectorRegistry

logger = logging.getLogger(__name__)

DEFAULT_INSTALL_ROOT = "~/.fusion-plugins-ecosystem/marketplace"
PACKAGE_KINDS = ("connector_pack", "skill_package")


@dataclass(frozen=True)
class MarketplaceEntry:
    """市场条目（索引中的一行）。"""

    id: str
    kind: str  # connector_pack | skill_package
    name: str
    version: str
    description: str = ""
    source: str | None = None  # git URL / 本地路径（内置包为 None）
    pack: dict[str, Any] | None = None  # connector_pack 内联定义（内置包）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source,
        }


class Marketplace:
    """连接器包/技能包市场：索引加载 + 安装/卸载/已装清单。"""

    def __init__(
        self,
        connector_registry: ConnectorRegistry,
        install_root: str | None = None,
        marketplace_index: str | None = None,
    ) -> None:
        self.connector_registry = connector_registry
        self.install_root = os.path.expanduser(install_root or DEFAULT_INSTALL_ROOT)
        self.marketplace_index = marketplace_index

    # ── 索引 ──

    def builtin_catalog(self) -> list[MarketplaceEntry]:
        """内置第一方连接器包目录（无需索引即可安装）。"""
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

    def load_index(
        self, source: str | None = None
    ) -> tuple[list[MarketplaceEntry], list[str]]:
        """加载市场索引，返回 (有效条目, 警告)。

        source：本地 JSON 路径或 HTTPS URL；None 用构造参数；两者皆空时
        仅返回内置目录。非法条目跳过并记警告，不让单条脏数据拖垮整个索引。
        """
        entries: list[MarketplaceEntry] = list(self.builtin_catalog())
        warnings: list[str] = []
        src = source or self.marketplace_index
        if not src:
            return entries, warnings
        try:
            raw = self._fetch_json(src)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            warnings.append(f"marketplace: 索引加载失败 {src!r}: {exc}")
            logger.warning("marketplace: %s", warnings[-1])
            return entries, warnings
        for item in raw.get("packages", []):
            if not isinstance(item, dict):
                warnings.append(f"marketplace: 跳过非对象条目 {item!r}")
                continue
            entry, err = self._validate_entry(item)
            if err:
                warnings.append(err)
                continue
            entries.append(entry)
        # 索引条目覆盖同 ID 内置条目（索引为准），保持目录内 ID 唯一
        deduped: dict[str, MarketplaceEntry] = {}
        for e in entries:
            deduped[e.id] = e
        final = list(deduped.values())
        logger.info("marketplace: 索引 %s 加载 %d 条", src, len(final))
        return final, warnings

    @staticmethod
    def _fetch_json(src: str) -> dict[str, Any]:
        if src.startswith("https://"):
            with urllib.request.urlopen(src, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        if src.startswith("http://"):
            raise ValueError("marketplace: 仅允许 HTTPS 索引（安全约束）")
        with open(os.path.expanduser(src), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _validate_entry(
        item: dict[str, Any],
    ) -> tuple[MarketplaceEntry | None, str | None]:
        pid = item.get("id")
        kind = item.get("kind")
        if not pid or not isinstance(pid, str):
            return None, "marketplace: 条目缺少 id"
        if kind not in PACKAGE_KINDS:
            return None, f"marketplace: {pid} 非法 kind={kind!r}"
        entry = MarketplaceEntry(
            id=pid,
            kind=kind,
            name=item.get("name", pid),
            version=str(item.get("version", "0.0.0")),
            description=item.get("description", ""),
            source=item.get("source"),
            pack=item.get("pack"),
        )
        if entry.kind == "connector_pack" and entry.pack is None and not entry.source:
            return None, f"marketplace: {pid} 既无内联 pack 也无 source"
        return entry, None

    # ── 安装 / 卸载 ──

    def install(self, entry_id: str, source: str | None = None) -> dict[str, Any]:
        """安装市场包，返回安装结果（含 kind 与产物路径/桥接插件 ID）。"""
        entries, _ = self.load_index(source)
        entry = next((e for e in entries if e.id == entry_id), None)
        if entry is None:
            raise KeyError(f"marketplace: 索引中无条目 {entry_id!r}")
        if entry.kind == "connector_pack":
            return self._install_connector_pack(entry)
        return self._install_skill_package(entry)

    def _install_connector_pack(self, entry: MarketplaceEntry) -> dict[str, Any]:
        pack_dict = entry.pack
        if pack_dict is None:
            pack_dir = self._materialize_source(entry)
            pack_file = os.path.join(pack_dir, "pack.json")
            if not os.path.exists(pack_file):
                raise ValueError(f"marketplace: {entry.id} 来源缺少 pack.json")
            with open(pack_file, encoding="utf-8") as f:
                pack_dict = json.load(f)
            pack_dict.setdefault("id", entry.id)
        pack = ConnectorPack.from_dict(pack_dict)
        plugin_ids = self.connector_registry.install_pack(pack)
        return {"kind": "connector_pack", "id": pack.id, "plugin_ids": plugin_ids}

    def _install_skill_package(self, entry: MarketplaceEntry) -> dict[str, Any]:
        if not entry.source:
            raise ValueError(f"marketplace: skill 包 {entry.id} 缺少 source")
        dest = os.path.join(self.install_root, entry.id)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        src = self._materialize_source(entry)
        shutil.copytree(src, dest)
        self.desk_log(f"skill 包 {entry.id} 已安装到 {dest}")
        return {"kind": "skill_package", "id": entry.id, "path": dest}

    def _materialize_source(self, entry: MarketplaceEntry) -> str:
        """把 entry.source 落成本地目录：git URL 浅克隆 / 本地路径直接返回。"""
        src = entry.source
        if not src:
            raise ValueError(f"marketplace: {entry.id} 缺少 source")
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
            raise ValueError("marketplace: 仅允许 HTTPS/本地来源（安全约束）")
        path = os.path.expanduser(src)
        if not os.path.isdir(path):
            raise ValueError(f"marketplace: {entry.id} 本地来源不存在: {src}")
        return path

    def uninstall(self, entry_id: str) -> None:
        """卸载市场包（连接器包注销桥接插件；技能包删除安装目录）。"""
        self.connector_registry.uninstall_pack(entry_id)
        dest = os.path.join(self.install_root, entry_id)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
            self.desk_log(f"{entry_id} 已卸载")

    def list_installed(self) -> list[dict[str, Any]]:
        """列出已安装包：连接器包 + 技能包目录。"""
        installed: list[dict[str, Any]] = []
        for pack in self.connector_registry.list_packs():
            installed.append(
                {"kind": "connector_pack", "id": pack.id, "version": pack.version}
            )
        if os.path.isdir(self.install_root):
            for name in sorted(os.listdir(self.install_root)):
                path = os.path.join(self.install_root, name)
                if name.startswith("_") or not os.path.isdir(path):
                    continue
                installed.append({"kind": "skill_package", "id": name, "path": path})
        return installed

    def desk_log(self, message: str) -> None:
        """经 Desk 记录市场操作日志（无 desk 时退化为模块日志）。"""
        desk = getattr(self.connector_registry, "desk", None)
        if desk is not None:
            desk.log("marketplace", "INFO", message)
        else:
            logger.info("marketplace: %s", message)
