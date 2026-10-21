"""外部 MCP 连接器框架（cowork 作 MCP client 消费任意外部 MCP server）。

对应 issue #27「Connector framework (MCP-client direction)」：
- ConnectorPack：声明式连接器包（transport/command/url/凭证/工具清单）
- ExternalMCPClient：最小 MCP client（stdio + Streamable HTTP），JSON-RPC 2.0
- ConnectorRegistry：安装/卸载连接器包，工具桥接为 PluginManifest（ cowork 节点），
  凭证经 DeskRuntime 加密存储（本地 Fernet），HIGH_RISK 工具需显式审批

约束：连接器工具固定 INLINE 沙箱（外部 MCP server 本身已是独立进程），
`sandbox_default_mode=process` 全局默认下禁止安装（闭包入口无法跨进程导入）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginRegistry,
)

logger = logging.getLogger(__name__)

# 单次 MCP 请求默认超时（秒）
MCP_REQUEST_TIMEOUT = 30.0
# MCP 握手协商的协议版本
MCP_CLIENT_VERSION = "2025-06-18"


class RiskLevel(str, Enum):
    """连接器工具风险等级（驱动审批闸门）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ConnectorToolSpec:
    """连接器工具声明（外部 MCP server 提供的能力子集）。"""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk: RiskLevel = RiskLevel.LOW
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk.value,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConnectorToolSpec":
        risk = data.get("risk", RiskLevel.LOW.value)
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            input_schema=dict(data.get("input_schema") or {}),
            risk=RiskLevel(risk)
            if risk in RiskLevel._value2member_map_
            else RiskLevel.LOW,
            read_only=bool(data.get("read_only", False)),
        )


@dataclass(frozen=True)
class ConnectorPack:
    """连接器包（声明式：外部 MCP server 地址/命令 + 凭证 + 工具清单）。"""

    id: str
    name: str
    version: str
    description: str
    transport: str  # "stdio" | "http"
    tools: tuple[ConnectorToolSpec, ...]
    command: tuple[str, ...] | None = None  # stdio: 启动外部 MCP server 的命令
    url: str | None = None  # http: Streamable HTTP 端点
    # 凭证：provider 名（Desk 加密存储键）→ 注入环境变量名（stdio）或头名（http）
    env_credentials: dict[str, str] = field(default_factory=dict)
    homepage: str = ""

    def __post_init__(self) -> None:
        if self.transport not in ("stdio", "http"):
            raise ValueError(
                f"connector {self.id!r}: transport 必须为 stdio/http, got {self.transport!r}"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"connector {self.id!r}: stdio 连接器必须提供 command")
        if self.transport == "http" and not self.url:
            raise ValueError(f"connector {self.id!r}: http 连接器必须提供 url")
        if not self.tools:
            raise ValueError(f"connector {self.id!r}: 至少声明一个工具")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "transport": self.transport,
            "command": list(self.command) if self.command else None,
            "url": self.url,
            "env_credentials": dict(self.env_credentials),
            "homepage": self.homepage,
            "tools": [t.to_dict() for t in self.tools],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConnectorPack":
        command = data.get("command")
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            transport=data.get("transport", "stdio"),
            tools=tuple(ConnectorToolSpec.from_dict(t) for t in data.get("tools", [])),
            command=tuple(command) if command else None,
            url=data.get("url"),
            env_credentials=dict(data.get("env_credentials") or {}),
            homepage=data.get("homepage", ""),
        )


class ExternalMCPClient:
    """最小 MCP client：对单个外部 MCP server 做 initialize/tools-list/tools-call。

    stdio：子进程 + 换行分隔 JSON-RPC（MCP stdio 传输规范）。
    http：Streamable HTTP，POST JSON-RPC，兼容 JSON 与 SSE（data: 行）响应。
    凭证经 DeskRuntime.get_api_key 解密注入（stdio→环境变量，http→Bearer 头）。
    """

    def __init__(
        self,
        pack: ConnectorPack,
        desk: DeskRuntime | None = None,
        timeout: float = MCP_REQUEST_TIMEOUT,
    ) -> None:
        self.pack = pack
        self.desk = desk
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._initialized = False

    # ── 凭证解析 ──

    def resolve_credentials(self) -> dict[str, str]:
        """从 Desk 加密存储解出凭证（缺失的 provider 记 warning 并跳过）。"""
        resolved: dict[str, str] = {}
        for provider, target in self.pack.env_credentials.items():
            key = self.desk.get_api_key(provider) if self.desk else None
            if key:
                resolved[target] = key
            else:
                logger.warning(
                    "connector: %s 缺少凭证 provider=%s（Desk 未配置，跳过注入）",
                    self.pack.id,
                    provider,
                )
        return resolved

    # ── JSON-RPC 基础 ──

    def _request_body(
        self, method: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        return body

    @staticmethod
    def _raise_rpc_error(resp: dict[str, Any]) -> dict[str, Any]:
        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            raise RuntimeError(
                f"MCP server 错误: {err.get('code')}: {err.get('message')}"
            )
        return resp.get("result", {})

    # ── stdio 传输 ──

    async def _ensure_stdio(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        env = {**os.environ, **self.resolve_credentials()}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.pack.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise RuntimeError(
                f"connector: {self.pack.id} 无法启动外部 MCP server {self.pack.command}: {exc}"
            ) from exc
        logger.info("connector: %s 外部 MCP server 已启动 (stdio)", self.pack.id)
        return self._proc

    async def _stdio_roundtrip(self, body: dict[str, Any]) -> dict[str, Any]:
        proc = await self._ensure_stdio()
        assert proc.stdin is not None and proc.stdout is not None
        line = json.dumps(body, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            if not raw:
                raise RuntimeError(
                    f"connector: {self.pack.id} 外部 MCP server 已退出 (code={proc.returncode})"
                )
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                resp = json.loads(stripped.decode("utf-8"))
            except json.JSONDecodeError:
                logger.debug("connector: 跳过非 JSON 输出行")
                continue
            if resp.get("id") == body["id"]:
                return self._raise_rpc_error(resp)
            logger.debug("connector: 忽略非匹配响应 id=%s", resp.get("id"))

    # ── http 传输（Streamable HTTP）──

    def _http_post_sync(self, body: dict[str, Any]) -> str:
        assert self.pack.url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        creds = self.resolve_credentials()
        token = creds.get("Authorization") or creds.get("authorization")
        if token:
            headers["Authorization"] = token
        req = urllib.request.Request(
            self.pack.url, data=json.dumps(body).encode("utf-8"), headers=headers
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")

    async def _http_roundtrip(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = await asyncio.to_thread(self._http_post_sync, body)
        except (urllib.error.URLError, OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"connector: {self.pack.id} HTTP 请求失败: {exc}"
            ) from exc
        # Streamable HTTP：JSON 或 SSE（data: 行）二选一
        if raw.lstrip().startswith("{"):
            resp = json.loads(raw)
            return self._raise_rpc_error(resp)
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                resp = json.loads(payload)
                if resp.get("id") == body["id"]:
                    return self._raise_rpc_error(resp)
        raise RuntimeError(f"connector: {self.pack.id} HTTP 响应中无匹配 JSON-RPC 结果")

    async def _roundtrip(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.pack.transport == "stdio":
            return await self._stdio_roundtrip(body)
        return await self._http_roundtrip(body)

    # ── MCP 方法 ──

    async def initialize(self) -> dict[str, Any]:
        """MCP initialize 握手（幂等）。"""
        if self._initialized:
            return {}
        result = await self._roundtrip(
            self._request_body(
                "initialize",
                {
                    "protocolVersion": MCP_CLIENT_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "fusion-plugins-ecosystem",
                        "version": "1.0",
                    },
                },
            )
        )
        if self.pack.transport == "stdio":
            proc = await self._ensure_stdio()
            assert proc.stdin is not None
            proc.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            await proc.stdin.drain()
        self._initialized = True
        logger.info(
            "connector: %s 握手完成 server=%s",
            self.pack.id,
            (result.get("serverInfo") or {}).get("name"),
        )
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """拉取外部 server 的 tools/list（连接器包工具清单的运行时校验源）。"""
        await self.initialize()
        result = await self._roundtrip(self._request_body("tools/list", {}))
        return list(result.get("tools", []))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用外部 server 的 tools/call，返回 MCP 标准 {content, isError}。"""
        await self.initialize()
        result = await self._roundtrip(
            self._request_body("tools/call", {"name": name, "arguments": arguments})
        )
        return {
            "content": result.get("content", []),
            "isError": bool(result.get("isError", False)),
        }

    async def close(self) -> None:
        """终止 stdio 子进程并复位会话态。"""
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None
        self._initialized = False


# JSON Schema type → PluginParamType（连接器工具入参桥接用）
_JSON_TYPE_TO_PARAM: dict[str, str] = {
    "string": "string",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "array": "array",
    "object": "object",
}


def _schema_to_params(input_schema: dict[str, Any]) -> tuple:
    """把工具 JSON Schema properties 桥接为 PluginParam 元组。"""
    from fusion_plugins_ecosystem.registry import PluginParam

    props = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    params = []
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        js_type = spec.get("type", "string")
        params.append(
            PluginParam(
                name=name,
                type=_JSON_TYPE_TO_PARAM.get(js_type, "string"),
                description=spec.get("description", ""),
                required=name in required,
            )
        )
    return tuple(params)


class _BoundTool:
    """连接器工具绑定入口：闭包 (pack, tool) → ConnectorRegistry.dispatch。

    仅限 INLINE 沙箱（外部 MCP server 已是独立进程，无需二次隔离）；
    闭包不可被 PROCESS worker 导入，由 ConnectorRegistry 安装期闸门兜底。
    """

    def __init__(
        self, registry: "ConnectorRegistry", pack_id: str, tool_name: str
    ) -> None:
        self.registry = registry
        self.pack_id = pack_id
        self.tool_name = tool_name

    async def __call__(self, desk: DeskRuntime, params: dict[str, Any]) -> Any:
        return await self.registry.dispatch(self.pack_id, self.tool_name, dict(params))


class ConnectorRegistry:
    """连接器注册中心：连接器包安装/卸载 + 工具桥接 + 审批闸门 + 持久化。

    用法：
        creg = ConnectorRegistry(desk=desk, registry=plugin_registry,
                                 persist_path="~/.fusion-ecosystem/connectors.json")
        creg.install_pack(feishu_pack)
        creg.approve_tool("feishu", "im.message.send")   # HIGH_RISK 工具需先审批
        # 桥接后的工具自动进入 PluginRegistry → MCP Tools / cowork 节点
    """

    def __init__(
        self,
        desk: DeskRuntime | None = None,
        registry: PluginRegistry | None = None,
        persist_path: str | None = None,
        enabled: bool = True,
        sandbox_default_mode: str = "inline",
        require_high_risk_approval: bool = True,
    ) -> None:
        self.desk = desk or DeskRuntime()
        self.registry = registry or PluginRegistry(self.desk)
        self.persist_path = persist_path
        self.enabled = enabled
        self.sandbox_default_mode = sandbox_default_mode
        self.require_high_risk_approval = require_high_risk_approval
        self._packs: dict[str, ConnectorPack] = {}
        self._approved: set[tuple[str, str]] = set()
        self._clients: dict[str, ExternalMCPClient] = {}
        if persist_path:
            self._load()

    # ── 安装 / 卸载 ──

    def install_pack(self, pack: ConnectorPack) -> list[str]:
        """安装连接器包：校验 + 桥接工具为 PluginManifest + 持久化。

        返回桥接后的插件 ID 列表。同版本重复安装幂等；版本变更先卸旧再装新。
        """
        if not self.enabled:
            raise RuntimeError(
                "connector: 连接器框架未启用（enable_connector_framework=false）"
            )
        if self.sandbox_default_mode == "process":
            raise RuntimeError(
                "connector: 连接器工具仅支持 inline 沙箱，"
                "请设置 sandbox_default_mode=inline 后重试"
            )
        existing = self._packs.get(pack.id)
        if existing is not None and existing.version == pack.version:
            logger.info("connector: %s v%s 重复安装（幂等忽略）", pack.id, pack.version)
            return self.pack_plugin_ids(pack.id)
        if existing is not None:
            self.uninstall_pack(pack.id)

        plugin_ids: list[str] = []
        for tool in pack.tools:
            manifest = self._tool_manifest(pack, tool)
            self.registry.register(manifest)
            plugin_ids.append(manifest.id)
        self._packs[pack.id] = pack
        self._save()
        self.desk.log(
            f"connector__{pack.id}",
            "INFO",
            f"连接器包已安装 v{pack.version}，桥接 {len(plugin_ids)} 个工具",
        )
        logger.info(
            "connector: %s v%s 已安装，工具: %s", pack.id, pack.version, plugin_ids
        )
        return plugin_ids

    def uninstall_pack(self, pack_id: str) -> None:
        """卸载连接器包：注销桥接插件 + 关闭客户端 + 持久化。"""
        pack = self._packs.pop(pack_id, None)
        if pack is None:
            return
        for tool in pack.tools:
            self.registry.unregister(self.plugin_tool_id(pack_id, tool.name))
        client = self._clients.pop(pack_id, None)
        if client is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.close())
            except RuntimeError:
                logger.debug("connector: 卸载 %s 无事件循环，跳过异步关闭", pack_id)
        self._save()
        self.desk.log(f"connector__{pack_id}", "INFO", "连接器包已卸载")
        logger.info("connector: %s 已卸载", pack_id)

    def _tool_manifest(
        self, pack: ConnectorPack, tool: ConnectorToolSpec
    ) -> PluginManifest:
        from fusion_plugins_ecosystem.schema import MCPAnnotations, SandboxMode

        return PluginManifest(
            id=self.plugin_tool_id(pack.id, tool.name),
            name=f"{pack.name} · {tool.name}",
            version=pack.version,
            category=PluginCategory.CONNECTOR,
            description=tool.description or f"{pack.name} 连接器工具 {tool.name}",
            capabilities=(PluginCapability.MCP_TOOL,),
            params=_schema_to_params(tool.input_schema),
            entry_point=_BoundTool(self, pack.id, tool.name),
            default_mounted=False,
            sandbox_mode=SandboxMode.INLINE,
            mcp_annotations=MCPAnnotations(
                readOnlyHint=tool.read_only,
                openWorldHint=True,
            ),
        )

    # ── 命名 / 查询 ──

    @staticmethod
    def plugin_tool_id(pack_id: str, tool_name: str) -> str:
        """连接器工具的桥接插件 ID：connector__<pack>__<tool>。"""
        return f"connector__{pack_id}__{tool_name}"

    def pack_plugin_ids(self, pack_id: str) -> list[str]:
        pack = self._packs.get(pack_id)
        if pack is None:
            return []
        return [self.plugin_tool_id(pack_id, t.name) for t in pack.tools]

    def get_pack(self, pack_id: str) -> ConnectorPack | None:
        return self._packs.get(pack_id)

    def list_packs(self) -> list[ConnectorPack]:
        return list(self._packs.values())

    def list_packs_as_dicts(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._packs.values()]

    def list_approved(self) -> list[tuple[str, str]]:
        return sorted(self._approved)

    # ── 审批闸门 ──

    def approve_tool(self, pack_id: str, tool_name: str) -> None:
        """批准 (pack, tool) 组合（HIGH_RISK 工具调用前必须批准）。"""
        self._approved.add((pack_id, tool_name))
        self._save()
        self.desk.log(f"connector__{pack_id}", "INFO", f"工具已审批: {tool_name}")

    def revoke_tool(self, pack_id: str, tool_name: str) -> None:
        """撤销审批。"""
        self._approved.discard((pack_id, tool_name))
        self._save()

    def is_approved(self, pack_id: str, tool_name: str) -> bool:
        return (pack_id, tool_name) in self._approved

    # ── 执行 ──

    def _client_for(self, pack: ConnectorPack) -> ExternalMCPClient:
        client = self._clients.get(pack.id)
        if client is None:
            client = ExternalMCPClient(pack, desk=self.desk)
            self._clients[pack.id] = client
        return client

    async def dispatch(
        self, pack_id: str, tool_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """执行连接器工具：风险闸门 → 外部 MCP tools/call。"""
        pack = self._packs.get(pack_id)
        if pack is None:
            raise KeyError(f"连接器 {pack_id!r} 未安装")
        tool = next((t for t in pack.tools if t.name == tool_name), None)
        if tool is None:
            raise KeyError(f"连接器 {pack_id!r} 无工具 {tool_name!r}")
        if (
            self.require_high_risk_approval
            and tool.risk == RiskLevel.HIGH
            and not self.is_approved(pack_id, tool_name)
        ):
            self.desk.log(
                f"connector__{pack_id}",
                "WARNING",
                f"HIGH_RISK 工具 {tool_name} 未审批，拒绝执行",
            )
            raise PermissionError(
                f"连接器工具 {pack_id}.{tool_name} 风险等级 HIGH 且未审批，"
                "请先调用 approve_tool 批准"
            )
        client = self._client_for(pack)
        try:
            result = await client.call_tool(tool_name, params)
            status = "success" if not result.get("isError") else "tool_error"
        except Exception:
            status = "error"
            self.desk.metrics.counter(
                "connector_tool_calls_total", "连接器工具调用总数（按结果分桶）"
            ).inc(connector=pack_id, status="error")
            raise
        self.desk.metrics.counter(
            "connector_tool_calls_total", "连接器工具调用总数（按结果分桶）"
        ).inc(connector=pack_id, status=status)
        return result

    async def close_all(self) -> None:
        """关闭全部连接器客户端（stdio 子进程回收）。"""
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()

    # ── 持久化 ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "packs": self.list_packs_as_dicts(),
            "approved": [[p, t] for p, t in sorted(self._approved)],
        }

    def _save(self) -> None:
        if not self.persist_path:
            return
        path = os.path.expanduser(self.persist_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load(self) -> None:
        path = os.path.expanduser(self.persist_path)  # type: ignore[arg-type]
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("connector: 持久化文件损坏，忽略 (%s)", exc)
            return
        for item in data.get("packs", []):
            try:
                pack = ConnectorPack.from_dict(item)
                self._packs[pack.id] = pack
                for tool in pack.tools:
                    self.registry.register(self._tool_manifest(pack, tool))
            except (ValueError, KeyError) as exc:
                logger.warning("connector: 跳过损坏的连接器包记录: %s", exc)
        for pair in data.get("approved", []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                self._approved.add((str(pair[0]), str(pair[1])))
        logger.info(
            "connector: 已恢复 %d 个连接器包, %d 条审批",
            len(self._packs),
            len(self._approved),
        )
