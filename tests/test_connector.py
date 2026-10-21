"""connector 框架测试：包校验/桥接/审批闸门/持久化/stdio+http client。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.connector import (
    ConnectorPack,
    ConnectorRegistry,
    ExternalMCPClient,
    RiskLevel,
)
from fusion_plugins_ecosystem.connector_packs import FEISHU_PACK


def _make_pack(tmp_pack_id: str = "test_conn") -> ConnectorPack:
    return ConnectorPack(
        id=tmp_pack_id,
        name="Test Connector",
        version="1.0.0",
        description="测试连接器",
        transport="stdio",
        command=("echo", "noop"),
        tools=(
            fpe.ConnectorToolSpec(
                name="read.thing",
                description="读",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string", "description": "查询"}},
                    "required": ["q"],
                },
                risk=RiskLevel.LOW,
                read_only=True,
            ),
            fpe.ConnectorToolSpec(
                name="send.thing",
                description="发",
                input_schema={"type": "object", "properties": {}},
                risk=RiskLevel.HIGH,
            ),
        ),
    )


# ── 包校验 ──


def test_pack_rejects_bad_transport() -> None:
    with pytest.raises(ValueError, match="transport"):
        ConnectorPack(
            id="x",
            name="x",
            version="1",
            description="",
            transport="grpc",
            tools=(fpe.ConnectorToolSpec(name="t", description=""),),
        )


def test_pack_stdio_requires_command() -> None:
    with pytest.raises(ValueError, match="command"):
        ConnectorPack(
            id="x",
            name="x",
            version="1",
            description="",
            transport="stdio",
            tools=(fpe.ConnectorToolSpec(name="t", description=""),),
        )


def test_pack_http_requires_url() -> None:
    with pytest.raises(ValueError, match="url"):
        ConnectorPack(
            id="x",
            name="x",
            version="1",
            description="",
            transport="http",
            tools=(fpe.ConnectorToolSpec(name="t", description=""),),
        )


def test_pack_requires_tools() -> None:
    with pytest.raises(ValueError, match="工具"):
        ConnectorPack(
            id="x",
            name="x",
            version="1",
            description="",
            transport="http",
            url="https://example.com/mcp",
            tools=(),
        )


def test_pack_roundtrip() -> None:
    pack = _make_pack()
    restored = ConnectorPack.from_dict(pack.to_dict())
    assert restored.id == pack.id
    assert restored.command == pack.command
    assert restored.tools[0].risk == RiskLevel.LOW
    assert restored.tools[1].risk == RiskLevel.HIGH


# ── 安装桥接 ──


def test_install_pack_bridges_tools_to_registry() -> None:
    creg = ConnectorRegistry()
    plugin_ids = creg.install_pack(_make_pack())
    assert plugin_ids == [
        "connector__test_conn__read.thing",
        "connector__test_conn__send.thing",
    ]
    manifest = creg.registry.get("connector__test_conn__read.thing")
    assert manifest is not None
    assert manifest.category == fpe.PluginCategory.CONNECTOR
    assert fpe.PluginCapability.MCP_TOOL in manifest.capabilities
    # JSON Schema properties 桥接为 PluginParam
    assert manifest.params[0].name == "q"
    assert manifest.params[0].required is True
    assert manifest.params[0].type == "string"


def test_install_bridged_tool_exposed_via_mcp_exporter() -> None:
    creg = ConnectorRegistry()
    creg.install_pack(_make_pack())
    exporter = fpe.MCPExporter(creg.registry, creg.desk)
    names = [t["name"] for t in exporter.list_tools()]
    assert "mcp__plugin__connector__test_conn__read.thing" in names


def test_install_disabled_framework_raises() -> None:
    creg = ConnectorRegistry(enabled=False)
    with pytest.raises(RuntimeError, match="未启用"):
        creg.install_pack(_make_pack())


def test_install_process_sandbox_guard() -> None:
    creg = ConnectorRegistry(sandbox_default_mode="process")
    with pytest.raises(RuntimeError, match="inline"):
        creg.install_pack(_make_pack())


def test_reinstall_same_version_idempotent() -> None:
    creg = ConnectorRegistry()
    ids1 = creg.install_pack(_make_pack())
    ids2 = creg.install_pack(_make_pack())
    assert ids1 == ids2
    assert len(creg.list_packs()) == 1


def test_reinstall_new_version_replaces() -> None:
    creg = ConnectorRegistry()
    creg.install_pack(_make_pack())
    upgraded = ConnectorPack.from_dict({**_make_pack().to_dict(), "version": "2.0.0"})
    creg.install_pack(upgraded)
    assert creg.get_pack("test_conn").version == "2.0.0"
    assert creg.registry.get("connector__test_conn__read.thing").version == "2.0.0"


def test_uninstall_pack_unregisters_tools() -> None:
    creg = ConnectorRegistry()
    creg.install_pack(_make_pack())
    creg.uninstall_pack("test_conn")
    assert creg.get_pack("test_conn") is None
    assert creg.registry.get("connector__test_conn__read.thing") is None


# ── 审批闸门 ──


async def test_high_risk_requires_approval() -> None:
    creg = ConnectorRegistry()
    creg.install_pack(_make_pack())
    with pytest.raises(PermissionError, match="HIGH"):
        await creg.dispatch("test_conn", "send.thing", {})
    creg.approve_tool("test_conn", "send.thing")
    assert creg.is_approved("test_conn", "send.thing")
    # 已审批：继续执行会走外部 server（command=echo 非 MCP，但不再触发审批闸门）
    with pytest.raises(RuntimeError, match="MCP server"):
        await creg.dispatch("test_conn", "send.thing", {})


def test_revoke_tool() -> None:
    creg = ConnectorRegistry()
    creg.approve_tool("test_conn", "send.thing")
    creg.revoke_tool("test_conn", "send.thing")
    assert not creg.is_approved("test_conn", "send.thing")


async def test_dispatch_unknown_pack_and_tool() -> None:
    creg = ConnectorRegistry()
    with pytest.raises(KeyError):
        await creg.dispatch("nope", "x", {})
    creg.install_pack(_make_pack())
    with pytest.raises(KeyError):
        await creg.dispatch("test_conn", "nope", {})


# ── 持久化 ──


def test_persistence_roundtrip(tmp_path) -> None:
    path = str(tmp_path / "connectors.json")
    creg = ConnectorRegistry(persist_path=path)
    creg.install_pack(_make_pack())
    creg.approve_tool("test_conn", "send.thing")

    creg2 = ConnectorRegistry(persist_path=path)
    assert creg2.get_pack("test_conn") is not None
    assert ("test_conn", "send.thing") in creg2.list_approved()
    # 恢复时桥接插件重新注册
    assert creg2.registry.get("connector__test_conn__read.thing") is not None


def test_persistence_corrupt_file_ignored(tmp_path) -> None:
    path = tmp_path / "connectors.json"
    path.write_text("{broken", encoding="utf-8")
    creg = ConnectorRegistry(persist_path=str(path))
    assert creg.list_packs() == []


# ── 凭证 ──


def test_resolve_credentials_from_desk_store() -> None:
    store: dict[str, str] = {}

    class FakeConfigCenter:
        def get(self, key):
            return store.get(key)

        def set(self, key, value):
            store[key] = value

    desk = fpe.DeskRuntime(config_center=FakeConfigCenter())
    desk.set_api_key("feishu_app_id", "cli_123")
    desk.set_api_key("feishu_app_secret", "sec_456")
    client = ExternalMCPClient(FEISHU_PACK, desk=desk)
    resolved = client.resolve_credentials()
    assert resolved == {"LARK_APP_ID": "cli_123", "LARK_APP_SECRET": "sec_456"}


def test_resolve_credentials_missing_skipped() -> None:
    client = ExternalMCPClient(FEISHU_PACK, desk=fpe.DeskRuntime())
    assert client.resolve_credentials() == {}


# ── stdio client（真实子进程回环）──

_ECHO_SERVER = """
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    m = req.get("method")
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "echo"}}
    elif m == "tools/list":
        res = {"tools": [{"name": "echo", "description": "echo tool"}]}
    elif m == "tools/call":
        res = {"content": [{"type": "text", "text": "echo:" + json.dumps(req["params"]["arguments"])}], "isError": False}
    else:
        res = {}
    out = {"jsonrpc": "2.0", "id": req.get("id"), "result": res}
    sys.stdout.write(json.dumps(out) + "\\n")
    sys.stdout.flush()
"""


async def test_stdio_client_roundtrip(tmp_path) -> None:
    server_py = tmp_path / "echo_mcp.py"
    server_py.write_text(_ECHO_SERVER, encoding="utf-8")
    pack = ConnectorPack(
        id="echo",
        name="Echo",
        version="1.0.0",
        description="",
        transport="stdio",
        command=(sys.executable, str(server_py)),
        tools=(fpe.ConnectorToolSpec(name="echo", description=""),),
    )
    client = ExternalMCPClient(pack, timeout=10)
    try:
        init = await client.initialize()
        assert init["serverInfo"]["name"] == "echo"
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"
        result = await client.call_tool("echo", {"k": "v"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == 'echo:{"k": "v"}'
    finally:
        await client.close()


async def test_stdio_client_bad_command_raises() -> None:
    pack = ConnectorPack(
        id="bad",
        name="Bad",
        version="1.0.0",
        description="",
        transport="stdio",
        command=("/nonexistent/binary/xyz",),
        tools=(fpe.ConnectorToolSpec(name="t", description=""),),
    )
    client = ExternalMCPClient(pack, timeout=5)
    with pytest.raises(RuntimeError, match="无法启动"):
        await client.initialize()


async def test_dispatch_over_real_stdio(tmp_path) -> None:
    server_py = tmp_path / "echo_mcp.py"
    server_py.write_text(_ECHO_SERVER, encoding="utf-8")
    pack = ConnectorPack(
        id="echo2",
        name="Echo2",
        version="1.0.0",
        description="",
        transport="stdio",
        command=(sys.executable, str(server_py)),
        tools=(
            fpe.ConnectorToolSpec(
                name="read.thing",
                description="",
                risk=RiskLevel.LOW,
            ),
        ),
    )
    creg = ConnectorRegistry()
    creg.install_pack(pack)
    result = await creg.dispatch("echo2", "read.thing", {"q": "hello"})
    assert "echo:" in result["content"][0]["text"]
    await creg.close_all()


# ── http client（本地线程 HTTP server 回环）──


def _start_rpc_http_server() -> tuple[HTTPServer, int]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {"content": [], "isError": False},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


async def test_http_client_roundtrip() -> None:
    server, port = _start_rpc_http_server()
    try:
        pack = ConnectorPack(
            id="http1",
            name="HTTP",
            version="1.0.0",
            description="",
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
            tools=(fpe.ConnectorToolSpec(name="t", description=""),),
        )
        client = ExternalMCPClient(pack, timeout=10)
        result = await client.call_tool("t", {})
        assert result["isError"] is False
    finally:
        server.shutdown()


async def test_http_client_unreachable_raises() -> None:
    pack = ConnectorPack(
        id="http2",
        name="HTTP",
        version="1.0.0",
        description="",
        transport="http",
        url="http://127.0.0.1:1/mcp",
        tools=(fpe.ConnectorToolSpec(name="t", description=""),),
    )
    client = ExternalMCPClient(pack, timeout=3)
    with pytest.raises(RuntimeError, match="HTTP 请求失败"):
        await client.call_tool("t", {})


def test_sse_response_parsing() -> None:
    raw = 'data: {"jsonrpc":"2.0","id":7,"result":{"ok":1}}\n\n'
    body = {"jsonrpc": "2.0", "id": 7, "method": "x"}
    lines = [
        line.strip()[len("data:") :].strip()
        for line in raw.splitlines()
        if line.strip().startswith("data:")
    ]
    assert len(lines) == 1
    resp = json.loads(lines[0])
    assert resp["id"] == body["id"] and resp["result"]["ok"] == 1
