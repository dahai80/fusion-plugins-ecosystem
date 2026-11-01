"""Extensions 控制台后端（issue #30 E3：Connectors + Plugins·MCP 两页签）。

通用化自 f-ecommerce agent_platform/extensions.py + mcp_intake.py +
plugin_bridge.py，剥离全部电商语义：

- Connectors：声明式 JSON 连接器包（rest_connector.py）。install = 全量
  静态校验 + 落盘；enable = 运行时桥接读通道（GET）为
  `connector__<id>__<op>` 能力插件（MCP_TOOL，INLINE），写通道不自动列出、
  进入人工审核队列；凭据经 Desk 加密存储（Fernet），调用时服务端解析，
  永不暴露给 agent。harness = 静态校验 + 用保险库凭据实测首个读操作。
- Plugins·MCP：插件 tick/untick（lifecycle enable/disable 对账）、
  声明式插件包安装（plugin.json 清单，不落地任意代码）、远端 MCP server
  probe & register（initialize + tools/list），tick 后的工具桥接为
  `mcp__<server>__<tool>` 能力插件，经 JSON-RPC tools/call 派发。

能力脊柱（spine）：由磁盘事实派生（连接器文档 + MCP ticks + 插件 ticks），
启动时 resync 重建桥接，进程内不保存第二真相源。
"""

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

DEFAULT_EXTENSIONS_DIR = str(DATA_DIR / "extensions")  # issue #38：数据目录单点解析
_MCP_KEY = "mcp_servers.json"
_TICKS_KEY = "plugin_ticks.json"
_PLUGINS_KEY = "plugins"
_MCP_TIMEOUT = 15.0


def _extensions_dir() -> Path:
    return Path(
        os.environ.get("FUSION_PLUGIN_EXTENSIONS_DIR", DEFAULT_EXTENSIONS_DIR)
    ).expanduser()


_VAULT_FILE = "vault.json"


def _local_vault_read() -> dict[str, Any]:
    """控制台本地加密保险库（Desk config_center 缺失时的回退存储）。"""
    path = _extensions_dir() / _VAULT_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("extensions_console: 本地保险库损坏，视为空")
        return {}


def _local_vault_write(data: dict[str, Any]) -> None:
    d = _extensions_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _VAULT_FILE
    tmp = d / (_VAULT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    path.chmod(0o600)


def _vault_credentials(desk: Any, credential_ref: str) -> dict[str, Any]:
    """从 Desk 加密存储解析凭据 JSON；缺失返回空 dict（调用方 fail-loud）。

    Desk config_center 缺失（standalone console）时回退控制台本地保险库。
    """
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


def _vault_headers(desk: Any, credential_ref: str) -> dict[str, str]:
    """保险库凭据 → 远端 MCP server 认证头。

    接受 {"headers": {...}} 原样注入，或 {"api_key"|"token"} → Bearer。
    ref 非空但库里没有 → fail-loud（探测期精确报错，不半注册）。
    """
    if not credential_ref:
        return {}
    creds = _vault_credentials(desk, credential_ref)
    if not creds:
        raise RuntimeError(
            f"no vault credentials under '{credential_ref}' — "
            "save them via PUT /api/adapters first"
        )
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
    """对 streamable-http MCP 端点做一次 JSON-RPC 往返。

    trust_env=False：daemon shell 里的代理变量会挂起 loopback 调用。
    """
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    import httpx  # console extra — 函数级导入，base 环境无 httpx 也可 import 本模块

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


def _manifest_from_decl(data: dict[str, Any]) -> Any:
    """声明式 plugin.json 清单 → PluginManifest（entry_point 必须是 module:attr）。"""
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
        PluginCapability(c)
        for c in (data.get("capabilities") or [])
        if c in PluginCapability._value2member_map_
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


class _RestOpBound:
    """REST 读操作绑定入口：闭包 → execute_operation（INLINE 专用）。"""

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


class _McpToolBound:
    """MCP intake 工具绑定入口：闭包 → tools/call（INLINE 专用）。"""

    def __init__(self, console: "ExtensionsConsole", server_id: str, tool: str) -> None:
        self.console = console
        self.server_id = server_id
        self.tool = tool

    async def __call__(self, desk: Any, params: dict[str, Any]) -> Any:
        result = await self.console.call_mcp_tool(
            self.server_id, self.tool, dict(params or {})
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "MCP tools/call failed")
        return result.get("result") or {}


class ExtensionsConsole:
    """Extensions 控制台服务（连接器 + 插件 + MCP intake + 能力脊柱派生）。"""

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
        # standalone console 未注入 lifecycle 时自建（tick/enable 桥接依赖它）
        self.lifecycle = lifecycle or PluginLifecycle(self.registry)
        self.installed_by = installed_by
        # issue #41：hub 面反映真实注册表 — console 与 fusion-plugin-server
        # 同源，自举内置插件（register 同版本幂等，注入侧已注册则 no-op）
        try:
            self.registry.register_builtin()
        except ValueError as exc:
            logger.warning("extensions_console: 内置插件注册冲突: %s", exc)

    # ── 持久化 ──

    def _read_json(self, name: str, default: Any) -> Any:
        path = _extensions_dir() / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("extensions_console: %s 损坏，返回默认值", name)
            return default

    def _write_json(self, name: str, data: Any) -> None:
        d = _extensions_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / name)

    # ── 能力脊柱（派生，无第二真相源）──

    def list_capabilities(self) -> list[dict[str, Any]]:
        caps: list[dict[str, Any]] = []
        for entry in connector_report():
            cid = entry["id"]
            doc = get_connector(cid) or {}
            disabled = bool(doc.get("_disabled"))
            for ch in read_channels(doc):
                # 脊柱由运行时桥接事实驱动：仅已 enable（桥接存在）或
                # 曾 enable 后 disable（_disabled 标记）的连接器入列
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
                continue  # 桥接清单由 connector/mcp 源覆盖，不重复计
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

    # ── 能力中心 hub（issue #41：四 tab + 分组 + 跨类搜索）──

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        """语义版本比较键：'1.2' < '1.10'，'1.0' 与 '1.0.0' 等价，非数字段按 0。"""
        parts = []
        for chunk in str(version or "0").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()  # 尾零归一：1.0.0 == 1.0
        return tuple(parts) or (0,)

    def _market_groups(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """市场目录 → (updates, recommended)：已装比对版本，未装进推荐位。

        只读目录（Marketplace(None)）— 安装动作归 PC-2；市场不可用时 hub 降级
        为空分组，不拖垮四 tab。
        """
        try:
            from fusion_plugins_ecosystem.marketplace import Marketplace

            mp = Marketplace(connector_registry=None)
            entries, warnings = mp.load_index()
            # 只读目录（registry=None）：list_installed 会走 connector_registry
            # .list_packs() — None 时降级为技能包目录扫描 + 连接器文档
            installed: dict[str, dict[str, Any]] = {}
            if mp.connector_registry is not None:
                installed = {i["id"]: i for i in mp.list_installed()}
            elif os.path.isdir(mp.install_root):
                for name in sorted(os.listdir(mp.install_root)):
                    path = os.path.join(mp.install_root, name)
                    if not name.startswith("_") and os.path.isdir(path):
                        installed[name] = {"id": name}
        except Exception as exc:  # noqa: BLE001 — 市场不可用不拖垮 hub
            logger.warning("extensions_console: 市场目录不可用: %s", exc)
            return [], []
        for entry in connector_report():
            installed.setdefault(entry["id"], entry)
        updates: list[dict[str, Any]] = []
        recommended: list[dict[str, Any]] = []
        for e in entries:
            if e.id in installed:
                current = str(installed[e.id].get("version") or "")
                if current and self._version_key(current) < self._version_key(
                    e.version
                ):
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
            logger.info("extensions_console: 市场索引警告: %s", warnings)
        return updates, recommended

    def list_hub(self, query: str = "") -> dict[str, Any]:
        """能力中心聚合视图：四 tab（plugins/skills/connectors/subagents）+
        已装/更新/推荐分组 + 跨类搜索（id/name/description 子串、大小写不敏感）。
        """
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
                continue  # 桥接清单由 connectors tab 覆盖，不重复计
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
            installed.append(
                {"id": m.id, "name": m.name, "type": "plugin", "enabled": enabled}
            )
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
            # 跨类搜索覆盖推荐位: 未装市场条目也命中（连接器包 →
            # connectors tab，技能包 → skills tab），打 recommended 标。
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

    # ── 安装/授权/更新/卸载流（issue #42 PC-2）──

    _BUNDLES_KEY = "bundles.json"

    def _bundles_state(self) -> dict[str, Any]:
        return self._read_json(self._BUNDLES_KEY, {})

    def _save_bundles_state(self, state: dict[str, Any]) -> None:
        self._write_json(self._BUNDLES_KEY, state)

    def _market_entry(self, entry_id: str) -> Any:
        from fusion_plugins_ecosystem.marketplace import Marketplace

        # 索引来源：FUSION_PLUGIN_MARKETPLACE_INDEX（本地 JSON / HTTPS），
        # 未设置时仅内置第一方目录
        mp = Marketplace(
            connector_registry=None,
            marketplace_index=os.environ.get("FUSION_PLUGIN_MARKETPLACE_INDEX") or None,
        )
        entries, _ = mp.load_index()
        entry = next((e for e in entries if e.id == entry_id), None)
        if entry is None:
            raise KeyError(f"市场索引中无条目 {entry_id!r}")
        return entry

    @staticmethod
    def _entry_items(entry: Any) -> list[dict[str, Any]]:
        """市场条目 → bundle items：bundle 展开声明 items；其余包单条目化。"""
        if entry.kind == "bundle":
            return list(entry.items or [])
        if entry.kind == "connector_pack":
            return [{"type": "connector_pack", "ref": entry.id}]
        return [{"type": "skill_package", "ref": entry.id}]

    def _connector_registry(self) -> Any:
        """bundle 安装 connector_pack 项用的 inline 连接器注册中心（惰性构建）。"""
        from fusion_plugins_ecosystem.connector import ConnectorRegistry

        return ConnectorRegistry(
            desk=self.desk,
            registry=self.registry,
            sandbox_default_mode="inline",
        )

    async def install_bundle(self, entry_id: str) -> dict[str, Any]:
        """一键安装市场包（issue #42）：skills 启用、子代理注册启用、
        连接器落盘并置 pending_auth（OAuth 后暴露）、connector_pack/skill_package
        走既有安装路径。逐项状态持久化，安装后可逐项停用。
        """
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
            except Exception as exc:  # noqa: BLE001 — 逐项报错不打断整包
                errors.append(f"{itype}:{ref}: {exc}")
                items_out.append(
                    {"type": itype, "ref": ref, "state": "error", "error": str(exc)}
                )
        state[entry.id] = {
            "id": entry.id,
            "name": entry.name,
            "version": entry.version,
            "changelog": entry.description,
            "kind": entry.kind,
            "items": items_out,
        }
        self._save_bundles_state(state)
        # issue #43：安装事件入审计管道（trace_id 串整包）
        trace = audit_events.new_trace_id()
        audit_events.emit(
            "hub.bundle.install",
            resource_type="bundle",
            resource_id=entry.id,
            details={
                "version": entry.version,
                "items": [
                    {"type": i["type"], "ref": i["ref"], "state": i["state"]}
                    for i in items_out
                ],
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
            f"bundle {entry.id} v{entry.version} 安装完成"
            f"（{len(items_out)} 项, {len(errors)} 错误）",
        )
        return {
            "installed": True,
            "bundle_id": entry.id,
            "version": entry.version,
            "items": items_out,
            "errors": errors,
        }

    async def _install_bundle_item(
        self, itype: str, ref: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        if itype in ("skill", "subagent"):
            manifest = self.registry.get(ref)
            if manifest is None:
                raise KeyError(f"插件 {ref!r} 未注册（先 install_plugin）")
            await self.lifecycle.enable(ref)
            return {"state": "enabled", "name": manifest.name}
        if itype == "connector":
            doc = dict(item.get("doc") or {})
            doc.setdefault("id", ref)
            problems = validate_connector(doc)
            if problems:
                raise ValueError(f"连接器 {ref} 校验失败: {problems}")
            doc.pop("_disabled", None)
            save_connector(ref, doc)
            creds = _vault_credentials(self.desk, str(doc.get("credential_ref") or ref))
            if creds:
                # 凭据已就绪 → 直接可用（等价 #30 validate → credentials → enable）
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
                    raise ValueError(f"连接器包 {ref} 无内联 pack 定义")
                pack = ConnectorPack.from_dict(pack_dict)
            plugin_ids = creg.install_pack(pack)
            # 桥接即暴露：安装路径与 tick-on 一致，工具置 ENABLED
            for pid in plugin_ids:
                try:
                    await self.lifecycle.enable(pid)
                except Exception as exc:  # noqa: BLE001 — 单工具失败不阻断整包
                    logger.warning(
                        "extensions_console: 启用包工具 %s 失败: %s", pid, exc
                    )
            self.desk.log(
                ref, "INFO", f"连接器包 {ref} 已安装（{len(plugin_ids)} 工具）"
            )
            return {"state": "enabled", "plugin_ids": plugin_ids}
        if itype == "skill_package":
            entry = self._market_entry(ref)
            from fusion_plugins_ecosystem.marketplace import Marketplace

            mp = Marketplace(connector_registry=self._connector_registry())
            out = mp._install_skill_package(entry)
            return {"state": "enabled", "path": out.get("path")}
        raise ValueError(f"未知 bundle item 类型 {itype!r}")

    def list_bundles(self) -> dict[str, Any]:
        state = self._bundles_state()
        # _scope_policy 是元数据键，不是 bundle — 不进列表
        return {"bundles": [v for k, v in state.items() if k != "_scope_policy"]}

    async def tick_bundle_item(
        self, bundle_id: str, ref: str, enabled: bool
    ) -> dict[str, Any]:
        """bundle 内逐项启停（issue #42：安装后 packed item 各自可停用）。"""
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"ticked": False, "error": f"bundle {bundle_id!r} 未安装"}
        item = next((i for i in bundle.get("items", []) if i.get("ref") == ref), None)
        if item is None:
            return {"ticked": False, "error": f"bundle {bundle_id} 无条目 {ref!r}"}
        if item.get("state") == "pending_auth" and enabled:
            return {"ticked": False, "error": f"{ref} 待授权（先完成 OAuth）"}
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
                pass  # 目录型技能包无运行时开关 — 状态仅记账
            item["state"] = "enabled" if enabled else "disabled"
            self._save_bundles_state(state)
            self.desk.log(
                bundle_id, "INFO", f"bundle 项 {ref} → {'启用' if enabled else '停用'}"
            )
            audit_events.emit(
                "hub.bundle.item.enable" if enabled else "hub.bundle.item.disable",
                resource_type=item["type"],
                resource_id=ref,
                details={"bundle": bundle_id},
                risk_level="low",
            )
            return {"ticked": True, "ref": ref, "state": item["state"]}
        except Exception as exc:  # noqa: BLE001 — 停用失败原样上报
            return {"ticked": False, "error": str(exc)}

    def _pack_plugin_ids(self, pack_id: str) -> list[str]:
        return [
            m.id
            for m in self.registry.list()
            if m.id.startswith(f"connector__{pack_id}__")
        ]

    # ── 连接器 OAuth（issue #42：hub 内完成，不跳出）──

    def oauth_scopes(self, connector_id: str) -> dict[str, Any]:
        """OAuth 首屏：读 scope（可勾选授权）+ 敏感 scope（逐用确认，不自动开）。"""
        doc = get_connector(connector_id)
        if not doc:
            return {"error": "connector not found"}
        # issue #43：标注实际授权状态（_scope_grants 缺省 = 全量，兼容 #30 直装路径）
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
        sensitive = [
            {"id": wid, "title": wid, "sensitive": True} for wid in write_channels(doc)
        ]
        auth = doc.get("auth") or {}
        fields = auth.get("credential_fields") or []
        return {
            "connector_id": connector_id,
            "scopes": scopes + sensitive,
            "credential_fields": fields,
        }

    async def oauth_complete(
        self, connector_id: str, scopes: list[str], credentials: dict[str, Any]
    ) -> dict[str, Any]:
        """OAuth 完成：凭据入保险库，勾选的读 scope 授权桥接，写 scope 记
        per_use 策略（绑定 cowork 审批系统，上游 fusion-cowork#225）。
        """
        doc = get_connector(connector_id)
        if not doc:
            return {"completed": False, "error": "connector not found"}
        if not isinstance(credentials, dict) or not credentials:
            return {"completed": False, "error": "credentials required"}
        valid_reads = {ch["id"] for ch in read_channels(doc)}
        valid_writes = set(write_channels(doc))
        unknown = [s for s in scopes if s not in valid_reads]
        if unknown:
            return {"completed": False, "error": f"未知读 scope: {unknown}"}
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
        # 敏感 scope 策略绑定：写通道不自动开，逐用确认（cowork #225 上游就绪前
        # 记录在案 + 查询接口可用，执行侧 #30 审核队列兜底）
        state = self._bundles_state()
        policy = state.setdefault("_scope_policy", {})
        policy[connector_id] = {wid: "per_use" for wid in sorted(valid_writes)}
        # bundle 内对应项 pending_auth → enabled
        for bundle in state.values():
            for item in bundle.get("items", []):  # type: ignore[union-attr]
                if (
                    isinstance(item, dict)
                    and item.get("type") == "connector"
                    and item.get("ref") == connector_id
                ):
                    item["state"] = "enabled"
        self._save_bundles_state(state)
        added = await self.enable_connector(connector_id)
        self.desk.log(
            f"connector__{connector_id}",
            "INFO",
            f"连接器 {connector_id} OAuth 完成：授权读 {len(granted)} 通道，"
            f"{len(valid_writes)} 写通道绑定逐用确认",
        )
        # issue #43：逐 scope 授权事件 + 敏感策略绑定事件（同一 trace_id）
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

    def scope_policy(self, connector_id: str) -> dict[str, Any]:
        """敏感 scope 策略查询（cowork 审批系统 / 权限引擎对接点）。"""
        state = self._bundles_state()
        policy = (state.get("_scope_policy") or {}).get(connector_id, {})
        return {"connector_id": connector_id, "per_use_scopes": sorted(policy)}

    # ── 更新流（issue #42：changelog 先行，新增项 opt-in）──

    def bundle_update_preview(self, bundle_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"error": f"bundle {bundle_id!r} 未安装"}
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
        new_items = [
            dict(item)
            for item in self._entry_items(entry)
            if item.get("ref") not in known_refs
        ]
        return {
            "bundle_id": bundle_id,
            "update_available": True,
            "installed_version": bundle.get("version"),
            "available_version": entry.version,
            "changelog": entry.description,
            "new_items": new_items,
        }

    async def bundle_update_apply(
        self, bundle_id: str, opt_in: list[str]
    ) -> dict[str, Any]:
        """应用更新：只装 opt-in 的新增项，版本号前移。"""
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
                continue  # 未勾选的新增项不自动开（opt-in 契约）
            try:
                item_state = await self._install_bundle_item(
                    str(item.get("type")), ref, item
                )
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
            f"bundle {bundle_id} 更新至 v{entry.version}"
            f"（opt-in {len(opted)}/{len(preview['new_items'])} 新增项）",
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

    # ── 卸载 + 影响面（issue #42：卸载前展示 被使用于）──

    def uninstall_preview(self, bundle_id: str) -> dict[str, Any]:
        state = self._bundles_state()
        bundle = state.get(bundle_id)
        if bundle is None:
            return {"error": f"bundle {bundle_id!r} 未安装"}
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

    def _reverse_lookup(self, refs: list[str]) -> list[dict[str, Any]]:
        """注册表反向查找：哪些 cowork 节点引用了这些能力（防御式遍历）。"""
        registry = getattr(self.desk, "node_registry", None)
        if registry is None or not refs:
            return []
        wanted = {r for r in refs if r}
        found: list[dict[str, Any]] = []
        try:
            nodes = registry.list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("extensions_console: 节点注册表不可读: %s", exc)
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
            return {"removed": False, "error": f"bundle {bundle_id!r} 未安装"}
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
                    for pid in self._pack_plugin_ids(ref):
                        if self._plugin_enabled(pid):
                            await self.lifecycle.disable(pid)
                    removed.append(ref)
                elif item.get("type") == "skill_package":
                    from fusion_plugins_ecosystem.marketplace import Marketplace

                    Marketplace(
                        connector_registry=self._connector_registry()
                    ).uninstall(ref)
                    removed.append(ref)
            except Exception as exc:  # noqa: BLE001 — 逐项报错不打断卸载
                errors.append(f"{item.get('type')}:{ref}: {exc}")
        self._save_bundles_state(state)
        self.desk.log(
            bundle_id,
            "INFO",
            f"bundle {bundle_id} 已卸载（{len(removed)} 项, {len(errors)} 错误）",
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

    # ── 审计视图 + scope 撤销 + 反向查找（issue #43 PC-3）──

    def audit_records(
        self,
        resource_id: str = "",
        resource_type: str = "",
        action: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """hub 内授权审计视图：谁在何时授权了什么（读 fusion-security 轨迹）。"""
        records = audit_events.query(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            trace_id=trace_id,
            limit=limit,
        )
        return {"records": records}

    async def revoke_scopes(
        self, connector_id: str, scopes: list[str]
    ) -> dict[str, Any]:
        """撤销已授权读 scope：更新 _scope_grants、重桥接、发审计事件。

        security 工作台侧的撤销动作经此落回注册表（下一载入即生效 —
        hub 每次 enable/列表都从盘上事实读取，无内存第二真相源）。
        """
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
            f"连接器 {connector_id} 撤销读 scope: {target}（余 {remaining}）",
        )
        return {
            "revoked": True,
            "connector_id": connector_id,
            "revoked_scopes": target,
            "remaining_scopes": remaining,
        }

    def capability_used_by(self, capability: str) -> dict[str, Any]:
        """被使用于：哪些 cowork 节点引用该能力（#42 反向查找的独立入口）。"""
        in_use_by = self._reverse_lookup([capability])
        return {"capability": capability, "in_use_by": in_use_by}

    # ── 稳定注册表读契约（issue #44 PC-4：单注册表三表面）──
    # 消费方：fusion-cowork#228 能力市场、fusion-agent-studio#529 构建器建议。
    # 契约版本 registry/1.0 — 只增字段不改语义；授权态只读，授权动作跳 hub。

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

    def _connector_grants(self, connector_id: str) -> list[str] | None:
        """已授权读 scope 列表；None = 全量授权（#30 直装路径，无 _scope_grants）。"""
        doc = get_connector(connector_id)
        if not doc:
            return None
        grants = doc.get("_scope_grants")
        if not isinstance(grants, list):
            return None
        return grants

    def registry_capabilities(self) -> dict[str, Any]:
        """registry/1.0 全量能力清单：单一事实源，消费方不得分叉缓存。

        连接器按文档读通道展开（非仅已桥接清单）— 已撤销/待授权通道以
        auth.state 呈现，消费方可见完整授权面而非只有活跃桥接。
        """
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
        # 连接器包（issue #27 ConnectorRegistry 桥接）无 rest 文档 — 按桥接
        # 清单入列，授权面 = 全量（包工具无逐 scope 模型，对齐 #30 直装路径）
        doc_cids = {entry["id"] for entry in connector_report()}
        for m in self.registry.list():
            if m.id.startswith("connector__"):
                cid = m.id.split("__", 2)[1]
                if cid in doc_cids:
                    continue  # rest 连接器由文档读通道展开覆盖
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
        logger.info("extensions_console: registry/1.0 清单 %d 项", len(items))
        return {"contract": self.REGISTRY_CONTRACT, "capabilities": items}

    def registry_capability(self, capability: str) -> dict[str, Any]:
        """registry/1.0 单能力详情：清单字段 + per-use 策略 + 被使用于。"""
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

    # ── 连接器 ──

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

    def install_connector(self, doc: dict[str, Any]) -> dict[str, Any]:
        """全量静态校验 + 落盘（安装记录即文件）。enable 才注册运行时。"""
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
            f"连接器 {cid} 已安装（{len(reads)} 读通道, {len(writes)} 写通道待审）",
        )
        logger.info(
            "extensions_console: 连接器 %s 已安装 by %s（%d 读, %d 写不自动列出）",
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

    def save_credentials(
        self, credential_ref: str, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        if not credential_ref or not isinstance(credentials, dict):
            return {"saved": False, "error": "missing credential_ref or credentials"}
        stored = json.dumps(credentials, ensure_ascii=False)
        # 首选 Desk 加密存储（cowork config_center, Fernet）；round-trip 验证
        # 可读性 —— standalone console（无 config_center）静默丢弃写入，回退
        # 控制台本地加密保险库（同 _encrypt_key 派生密钥）。
        if hasattr(self.desk, "set_api_key"):
            self.desk.set_api_key(credential_ref, stored)
            if (
                hasattr(self.desk, "get_api_key")
                and self.desk.get_api_key(credential_ref) == stored
            ):
                logger.info(
                    "extensions_console: 凭据 %s 已写入 Desk 加密存储", credential_ref
                )
                return {"saved": True, "credential_ref": credential_ref}
        try:
            from fusion_plugins_ecosystem.desk_runtime import _encrypt_key
        except ImportError:
            return {
                "saved": False,
                "error": "vault unavailable (no Desk config center, "
                "no local encryption)",
            }
        vault = _local_vault_read()
        vault[credential_ref] = _encrypt_key(stored)
        _local_vault_write(vault)
        logger.info(
            "extensions_console: 凭据 %s 已写入控制台本地加密保险库", credential_ref
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
            f"连接器 {connector_id} 已启用，自动列出 {len(added)} 个读能力",
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
            f"连接器 {connector_id} 已禁用，{len(affected)} 个能力翻转",
        )
        return {
            "disabled": True,
            "connector_id": connector_id,
            "capabilities_disabled": affected,
        }

    async def remove_connector(self, connector_id: str) -> dict[str, Any]:
        """删除连接器：摘桥接 + 删文档 + 清 scope 策略（issue #43 补全生命周期）。"""
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
            f"连接器 {connector_id} 已删除，摘除 {len(affected)} 个能力",
        )
        return {
            "removed": True,
            "connector_id": connector_id,
            "capabilities_removed": affected,
        }

    async def harness(self, connector_id: str) -> dict[str, Any]:
        """一致性 harness：静态校验 + 保险库凭据实测首个读操作。"""
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
                    f"{reads[0]['id']} live check failed: "
                    f"{result.get('error_code')}: {result.get('reason')}"
                )
        return {
            "connector_id": connector_id,
            "problems": problems,
            "conformant": not problems,
        }

    # ── 桥接（连接器读通道 → 能力插件）──

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
        # issue #42：OAuth 逐 scope 授权 — 只桥接已授权读通道
        # （doc._scope_grants 缺省 = 全量，兼容 #30 直装直启路径）
        granted = doc.get("_scope_grants")
        for ch in read_channels(doc):
            if isinstance(granted, list) and ch["id"] not in granted:
                logger.info(
                    "extensions_console: 连接器 %s 读通道 %s 未授权，跳过桥接",
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
                    mcp_annotations=MCPAnnotations(
                        readOnlyHint=True, openWorldHint=True
                    ),
                )
            )
            added.append(f"connector.{cid}.{ch['id']}")
        await self._enable_all(f"connector__{cid}__")
        logger.info("extensions_console: 连接器 %s 桥接 %d 个读通道", cid, len(added))
        return added

    async def _enable_all(self, prefix: str) -> None:
        """桥接即暴露：读通道/已 tick 工具注册后置 ENABLED（auto-list 契约）。"""
        if self.lifecycle is None:
            return
        for m in list(self.registry.list()):
            if m.id.startswith(prefix):
                try:
                    await self.lifecycle.enable(m.id)
                except Exception as exc:  # noqa: BLE001 — 逐个报错不打断批次
                    logger.warning("extensions_console: 启用 %s 失败: %s", m.id, exc)

    async def _unbridge_connector(self, connector_id: str) -> list[str]:
        removed = []
        prefix = f"connector__{connector_id}__"
        for manifest in list(self.registry.list()):
            if manifest.id.startswith(prefix):
                if self.lifecycle is not None:
                    try:
                        await self.lifecycle.disable(manifest.id)
                    except Exception:  # noqa: BLE001 — disable 失败仍注销
                        pass
                self.registry.unregister(manifest.id)
                removed.append(manifest.id)
        return removed

    # ── 插件（tick/untick 对账 + 声明式安装）──

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

    async def tick_nodes(self, plugin_base: str, nodes: list[str]) -> dict[str, Any]:
        """Apply ticks：勾选集合对账 —— 勾选的 enable，未勾的 disable。"""
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
            except Exception as exc:  # noqa: BLE001 — 逐节点报错不打断批次
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
                    except Exception:  # noqa: BLE001 — disable 失败仍继续注销
                        pass
                self.registry.unregister(m.id)
                removed.append(m.id)
        self._persist_ticks(plugin_base, [])
        custom = _extensions_dir() / _PLUGINS_KEY / f"{plugin_base}.json"
        if custom.exists():
            custom.unlink()
        self.desk.log(
            plugin_base, "INFO", f"插件 {plugin_base} 已移除（{len(removed)} 能力）"
        )
        return {"removed": bool(removed), "capabilities_removed": removed}

    def install_plugin(self, source: str) -> dict[str, Any]:
        """安装声明式插件包：目录或 zip，含 plugin.json 清单（不落地任意代码）。"""
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
            (store / f"{manifest.id}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        nodes = [self._node_of(manifest.id) or manifest.id]
        self._persist_ticks(manifest.id, [])
        self.desk.log(
            manifest.id,
            "INFO",
            f"声明式插件 {manifest.id} 已安装，候选节点: {nodes}（tick 后暴露）",
        )
        return {
            "installed": True,
            "plugin": manifest.id,
            "candidate_nodes": nodes,
            "note": "candidates only — tick to expose",
        }

    # ── 远端 MCP intake ──

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        ticks: dict[str, list[str]] = data.get("ticks") or {}
        out = []
        for s in data.get("servers") or []:
            entry = dict(s)
            entry["ticked"] = sorted(ticks.get(s.get("id", ""), []))
            out.append(entry)
        return out

    def register_mcp_server(
        self, server_id: str, url: str, credential_ref: str = ""
    ) -> dict[str, Any]:
        """probe（initialize + tools/list）通过才落盘 —— 先证明端点会说 MCP。"""
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
        except Exception as exc:  # noqa: BLE001 — 诚实 probe envelope
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
        data["servers"] = [
            s for s in data.get("servers") or [] if s.get("id") != server_id
        ]
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

    async def tick_mcp_tools(self, server_id: str, tools: list[str]) -> dict[str, Any]:
        """tick 工具 → 桥接为 mcp__<server>__<tool> 能力插件（重 tick 全量对账）。"""
        server = next(
            (s for s in self.list_mcp_servers() if s.get("id") == server_id), None
        )
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
                "extensions_console: MCP tick 部分失败 %s: %s", server_id, errors
            )
        return {"ticked": ticked, "errors": errors}

    def _persist_mcp_ticks(self, server_id: str, tools: list[str]) -> None:
        """MCP ticks 与 servers 同文件（mcp_servers.json），单一真相源。"""
        data = self._read_json(_MCP_KEY, {"servers": [], "ticks": {}})
        ticks = data.get("ticks") or {}
        if tools:
            ticks[server_id] = sorted(set(tools))
        else:
            ticks.pop(server_id, None)
        data["ticks"] = ticks
        self._write_json(_MCP_KEY, data)

    async def _bridge_mcp_tools(
        self, server: dict[str, Any], tools: list[str]
    ) -> list[str]:
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
                    except Exception:  # noqa: BLE001 — disable 失败仍注销
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
                    mcp_annotations=MCPAnnotations(
                        readOnlyHint=read_only, openWorldHint=True
                    ),
                )
            )
            bridged.append(f"mcp.{sid}.{name}")
        await self._enable_all(prefix)
        logger.info(
            "extensions_console: MCP server %s 桥接 %d 个工具", sid, len(bridged)
        )
        return bridged

    async def call_mcp_tool(
        self, server_id: str, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """门侧执行：tools/call 派发到注册 URL，认证头服务端解析。"""
        server = next(
            (s for s in self.list_mcp_servers() if s.get("id") == server_id), None
        )
        if not server:
            return {"ok": False, "error": f"MCP server '{server_id}' not registered"}
        try:
            auth_headers = _vault_headers(
                self.desk, str(server.get("credential_ref") or "")
            )
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
        except Exception as exc:  # noqa: BLE001 — 诚实 envelope
            return {
                "ok": False,
                "error": f"tools/call failed: {type(exc).__name__}: {exc}"[:300],
            }
        return {"ok": True, "result": result}

    # ── 启动 resync（磁盘事实重建运行时桥接）──

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
                    self.registry.register(
                        _manifest_from_decl(
                            json.loads(path.read_text(encoding="utf-8"))
                        )
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "extensions_console: 跳过损坏插件清单 %s: %s", path.name, exc
                    )
        # issue #44：已装 bundle 的 enabled 项跨重启重建（连接器包桥接/技能启用
        # 是运行时态，不重建则消费方下次拉取即"消失"）
        for bundle in self.list_bundles()["bundles"]:
            for item in bundle.get("items", []):
                if item.get("state") != "enabled":
                    continue
                itype = str(item.get("type"))
                ref = str(item.get("ref"))
                try:
                    if itype == "connector_pack":
                        if self._pack_plugin_ids(ref):
                            continue  # 桥接已存在（重复 resync）— 不重装
                        out = await self._install_bundle_item(itype, ref, {})
                        relisted.extend(out.get("plugin_ids", []))
                    elif itype in ("skill", "subagent") and self.registry.get(ref):
                        if not self._plugin_enabled(ref):
                            await self.lifecycle.enable(ref)
                        relisted.append(ref)
                except Exception as exc:  # noqa: BLE001 — 单项失败不拖垮 resync
                    logger.warning(
                        "extensions_console: resync 重建 bundle 项 %s:%s 失败: %s",
                        bundle.get("id"),
                        ref,
                        exc,
                    )
        logger.info("extensions_console: resync 重建 %d 个能力", len(relisted))
        return {"relisted": relisted}
