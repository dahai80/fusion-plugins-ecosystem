# External MCP connector framework (cowork consumes arbitrary external MCP
# servers as an MCP client).
# Corresponds to issue #27 "Connector framework (MCP-client direction)":
# - ConnectorPack: declarative connector pack (transport/command/url/credentials/tool list)
# - ExternalMCPClient: minimal MCP client (stdio + Streamable HTTP), JSON-RPC 2.0
# - ConnectorRegistry: install/uninstall connector packs, bridge tools into
#   PluginManifest (cowork nodes), credentials encrypted via DeskRuntime
#   (local Fernet), HIGH_RISK tools require explicit approval
# Constraint: connector tools are pinned to the INLINE sandbox (the external
# MCP server is already a separate process); installs are refused when the
# global default is `sandbox_default_mode=process` (closure entry points
# cannot be imported cross-process).

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

# Default per-request MCP timeout (seconds)
MCP_REQUEST_TIMEOUT = 30.0
# Protocol version negotiated during the MCP handshake
MCP_CLIENT_VERSION = "2025-06-18"


# Connector tool risk level (drives the approval gate).
class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
# Connector tool declaration (a subset of capabilities provided by the
# external MCP server).
class ConnectorToolSpec:
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
    def from_dict(cls, data: dict[str, Any]) -> ConnectorToolSpec:
        risk = data.get("risk", RiskLevel.LOW.value)
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            input_schema=dict(data.get("input_schema") or {}),
            risk=RiskLevel(risk) if risk in RiskLevel._value2member_map_ else RiskLevel.LOW,
            read_only=bool(data.get("read_only", False)),
        )


@dataclass(frozen=True)
# Connector pack (declarative: external MCP server address/command +
# credentials + tool list).
class ConnectorPack:
    id: str
    name: str
    version: str
    description: str
    transport: str  # "stdio" | "http"
    tools: tuple[ConnectorToolSpec, ...]
    command: tuple[str, ...] | None = None  # stdio: command launching the external MCP server
    url: str | None = None  # http: Streamable HTTP endpoint
    # Credentials: provider name (Desk encrypted storage key) → env var name
    # (stdio) or header name (http)
    env_credentials: dict[str, str] = field(default_factory=dict)
    homepage: str = ""

    def __post_init__(self) -> None:
        if self.transport not in ("stdio", "http"):
            raise ValueError(f"connector {self.id!r}: transport must be stdio/http, got {self.transport!r}")
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"connector {self.id!r}: stdio connector requires a command")
        if self.transport == "http" and not self.url:
            raise ValueError(f"connector {self.id!r}: http connector requires a url")
        if not self.tools:
            raise ValueError(f"connector {self.id!r}: must declare at least one tool")

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
    def from_dict(cls, data: dict[str, Any]) -> ConnectorPack:
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
    # Minimal MCP client: performs initialize/tools-list/tools-call against a
    # single external MCP server.
    # stdio: subprocess + newline-delimited JSON-RPC (MCP stdio transport spec).
    # http: Streamable HTTP, POST JSON-RPC, accepts both JSON and SSE
    # (data: line) responses.
    # Credentials are decrypted via DeskRuntime.get_api_key and injected
    # (stdio → env vars, http → Bearer header).
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

    # ── Credential resolution ──

    # Resolve credentials from Desk encrypted storage (missing providers log
    # a warning and are skipped).
    def resolve_credentials(self) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for provider, target in self.pack.env_credentials.items():
            key = self.desk.get_api_key(provider) if self.desk else None
            if key:
                resolved[target] = key
            else:
                logger.warning(
                    "connector: %s missing credential provider=%s (not configured in Desk, skipping injection)",
                    self.pack.id,
                    provider,
                )
        return resolved

    # ── JSON-RPC basics ──

    def _request_body(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        return body

    @staticmethod
    def _raise_rpc_error(resp: dict[str, Any]) -> dict[str, Any]:
        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            raise RuntimeError(f"MCP server error: {err.get('code')}: {err.get('message')}")
        return resp.get("result", {})

    # ── stdio transport ──

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
                f"connector: {self.pack.id} failed to launch external MCP server {self.pack.command}: {exc}"
            ) from exc
        logger.info("connector: %s external MCP server started (stdio)", self.pack.id)
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
                raise RuntimeError(f"connector: {self.pack.id} external MCP server exited (code={proc.returncode})")
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                resp = json.loads(stripped.decode("utf-8"))
            except json.JSONDecodeError:
                logger.debug("connector: skipping non-JSON output line")
                continue
            if resp.get("id") == body["id"]:
                return self._raise_rpc_error(resp)
            logger.debug("connector: ignoring response with unmatched id=%s", resp.get("id"))

    # ── http transport (Streamable HTTP) ──

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
        req = urllib.request.Request(self.pack.url, data=json.dumps(body).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")

    async def _http_roundtrip(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = await asyncio.to_thread(self._http_post_sync, body)
        except (urllib.error.URLError, OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"connector: {self.pack.id} HTTP request failed: {exc}") from exc
        # Streamable HTTP: either JSON or SSE (data: line)
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
        raise RuntimeError(f"connector: {self.pack.id} no matching JSON-RPC result in HTTP response")

    async def _roundtrip(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.pack.transport == "stdio":
            return await self._stdio_roundtrip(body)
        return await self._http_roundtrip(body)

    # ── MCP methods ──

    # MCP initialize handshake (idempotent).
    async def initialize(self) -> dict[str, Any]:
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
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await proc.stdin.drain()
        self._initialized = True
        logger.info(
            "connector: %s handshake complete server=%s",
            self.pack.id,
            (result.get("serverInfo") or {}).get("name"),
        )
        return result

    # Fetch tools/list from the external server (the runtime validation source
    # for the connector pack's tool list).
    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self._roundtrip(self._request_body("tools/list", {}))
        return list(result.get("tools", []))

    # Call tools/call on the external server, returning the MCP-standard
    # {content, isError}.
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        result = await self._roundtrip(self._request_body("tools/call", {"name": name, "arguments": arguments}))
        return {
            "content": result.get("content", []),
            "isError": bool(result.get("isError", False)),
        }

    # Terminate the stdio subprocess and reset session state.
    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None
        self._initialized = False


# JSON Schema type → PluginParamType (for bridging connector tool params)
_JSON_TYPE_TO_PARAM: dict[str, str] = {
    "string": "string",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "array": "array",
    "object": "object",
}


# Bridge tool JSON Schema properties into a PluginParam tuple.
def _schema_to_params(input_schema: dict[str, Any]) -> tuple:
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
    # Connector tool bound entry point: closure (pack, tool) →
    # ConnectorRegistry.dispatch.
    # INLINE sandbox only (the external MCP server is already a separate
    # process, no double isolation needed); closures cannot be imported by
    # PROCESS workers — the ConnectorRegistry install-time gate enforces this.
    def __init__(self, registry: ConnectorRegistry, pack_id: str, tool_name: str) -> None:
        self.registry = registry
        self.pack_id = pack_id
        self.tool_name = tool_name

    async def __call__(self, desk: DeskRuntime, params: dict[str, Any]) -> Any:
        return await self.registry.dispatch(self.pack_id, self.tool_name, dict(params))


class ConnectorRegistry:
    # Connector registry: pack install/uninstall + tool bridging + approval
    # gate + persistence.
    # Usage:
    #     creg = ConnectorRegistry(desk=desk, registry=plugin_registry,
    #                              persist_path="~/.fusion-ecosystem/connectors.json")
    #     creg.install_pack(feishu_pack)
    #     creg.approve_tool("feishu", "im.message.send")   # HIGH_RISK tools need approval first
    #     # Bridged tools automatically appear in PluginRegistry → MCP Tools / cowork nodes
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

    # ── Install / uninstall ──

    # Install a connector pack: validate + bridge tools into PluginManifest +
    # persist.
    # Returns the list of bridged plugin IDs. Re-installing the same version
    # is idempotent; a version change uninstalls the old pack first.
    def install_pack(self, pack: ConnectorPack) -> list[str]:
        if not self.enabled:
            raise RuntimeError("connector: connector framework not enabled (enable_connector_framework=false)")
        if self.sandbox_default_mode == "process":
            raise RuntimeError(
                "connector: connector tools only support the inline sandbox, set sandbox_default_mode=inline and retry"
            )
        existing = self._packs.get(pack.id)
        if existing is not None and existing.version == pack.version:
            logger.info("connector: %s v%s duplicate install (idempotent, ignored)", pack.id, pack.version)
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
            f"connector pack installed v{pack.version}, bridged {len(plugin_ids)} tools",
        )
        logger.info("connector: %s v%s installed, tools: %s", pack.id, pack.version, plugin_ids)
        return plugin_ids

    # Uninstall a connector pack: unregister bridged plugins + close clients +
    # persist.
    def uninstall_pack(self, pack_id: str) -> None:
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
                logger.debug("connector: no event loop while uninstalling %s, skipping async close", pack_id)
        self._save()
        self.desk.log(f"connector__{pack_id}", "INFO", "connector pack uninstalled")
        logger.info("connector: %s uninstalled", pack_id)

    def _tool_manifest(self, pack: ConnectorPack, tool: ConnectorToolSpec) -> PluginManifest:
        from fusion_plugins_ecosystem.schema import MCPAnnotations, SandboxMode

        return PluginManifest(
            id=self.plugin_tool_id(pack.id, tool.name),
            name=f"{pack.name} · {tool.name}",
            version=pack.version,
            category=PluginCategory.CONNECTOR,
            description=tool.description or f"{pack.name} connector tool {tool.name}",
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

    # ── Naming / lookup ──

    # Bridged plugin ID for a connector tool: connector__<pack>__<tool>.
    @staticmethod
    def plugin_tool_id(pack_id: str, tool_name: str) -> str:
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

    # ── Approval gate ──

    # Approve a (pack, tool) pair (HIGH_RISK tools must be approved before calls).
    def approve_tool(self, pack_id: str, tool_name: str) -> None:
        self._approved.add((pack_id, tool_name))
        self._save()
        self.desk.log(f"connector__{pack_id}", "INFO", f"tool approved: {tool_name}")

    # Revoke an approval.
    def revoke_tool(self, pack_id: str, tool_name: str) -> None:
        self._approved.discard((pack_id, tool_name))
        self._save()

    def is_approved(self, pack_id: str, tool_name: str) -> bool:
        return (pack_id, tool_name) in self._approved

    # ── Execution ──

    def _client_for(self, pack: ConnectorPack) -> ExternalMCPClient:
        client = self._clients.get(pack.id)
        if client is None:
            client = ExternalMCPClient(pack, desk=self.desk)
            self._clients[pack.id] = client
        return client

    async def dispatch(self, pack_id: str, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        # Execute a connector tool: risk gate → external MCP tools/call.
        pack = self._packs.get(pack_id)
        if pack is None:
            raise KeyError(f"connector {pack_id!r} not installed")
        tool = next((t for t in pack.tools if t.name == tool_name), None)
        if tool is None:
            raise KeyError(f"connector {pack_id!r} has no tool {tool_name!r}")
        if self.require_high_risk_approval and tool.risk == RiskLevel.HIGH and not self.is_approved(pack_id, tool_name):
            self.desk.log(
                f"connector__{pack_id}",
                "WARNING",
                f"HIGH_RISK tool {tool_name} not approved, refusing execution",
            )
            raise PermissionError(
                f"connector tool {pack_id}.{tool_name} has risk level HIGH and is not approved, call approve_tool first"
            )
        client = self._client_for(pack)
        try:
            result = await client.call_tool(tool_name, params)
            status = "success" if not result.get("isError") else "tool_error"
        except Exception:
            status = "error"
            self.desk.metrics.counter(
                "connector_tool_calls_total", "connector tool calls total (bucketed by result)"
            ).inc(connector=pack_id, status="error")
            raise
        self.desk.metrics.counter("connector_tool_calls_total", "connector tool calls total (bucketed by result)").inc(
            connector=pack_id, status=status
        )
        return result

    # Close all connector clients (reap stdio subprocesses).
    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()

    # ── Persistence ──

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
            logger.warning("connector: corrupt persistence file, ignoring (%s)", exc)
            return
        for item in data.get("packs", []):
            try:
                pack = ConnectorPack.from_dict(item)
                self._packs[pack.id] = pack
                for tool in pack.tools:
                    self.registry.register(self._tool_manifest(pack, tool))
            except (ValueError, KeyError) as exc:
                logger.warning("connector: skipping corrupt connector pack record: %s", exc)
        for pair in data.get("approved", []):
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                self._approved.add((str(pair[0]), str(pair[1])))
        logger.info(
            "connector: restored %d connector packs, %d approvals",
            len(self._packs),
            len(self._approved),
        )
