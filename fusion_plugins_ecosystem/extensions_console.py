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

from fusion_plugins_ecosystem.config import DATA_DIR
from fusion_plugins_ecosystem.rest_connector import (
    connector_report,
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
        self._unbridge_connector(cid)
        added = []
        for ch in read_channels(doc):
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
        logger.info("extensions_console: resync 重建 %d 个能力", len(relisted))
        return {"relisted": relisted}
