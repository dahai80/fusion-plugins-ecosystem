"""MCP 传输层 + Server 测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.server import MCPServer
from fusion_plugins_ecosystem.transport import (
    HTTPTransport,
    SSETransport,
    StdioTransport,
    create_transport,
)


# ── create_transport 工厂 ──


def test_create_transport_stdio() -> None:
    t = create_transport("stdio")
    assert isinstance(t, StdioTransport)


def test_create_transport_sse() -> None:
    t = create_transport("sse", host="0.0.0.0", port=9000)
    assert isinstance(t, SSETransport)


def test_create_transport_http() -> None:
    t = create_transport("http", port=8080)
    assert isinstance(t, HTTPTransport)


def test_create_transport_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown transport"):
        create_transport("websocket")


# ── StdioTransport basic ──


def test_stdio_transport_init() -> None:
    t = StdioTransport()
    assert t._running is False


def test_stdio_transport_set_handler() -> None:
    t = StdioTransport()
    called = []
    t.set_handler(lambda r: called.append(r))
    assert t._handler is not None


# ── SSETransport ──


async def test_sse_transport_start_stop() -> None:
    t = SSETransport(host="127.0.0.1", port=0)
    await t.start()
    assert t._running is True
    assert t.port > 0
    await t.stop()
    assert t._running is False


async def test_sse_send_to_missing_session_noop() -> None:
    t = SSETransport()
    await t.send_to_session("nonexistent", {"test": True})


# ── HTTPTransport ──


async def test_http_transport_start_stop() -> None:
    t = HTTPTransport(host="127.0.0.1", port=0)
    await t.start()
    assert t._running is True
    assert t.port > 0
    await t.stop()
    assert t._running is False


async def test_http_send_is_noop() -> None:
    t = HTTPTransport()
    await t.send({"test": True})


# ── HTTPTransport request/response ──


async def test_http_transport_request_response() -> None:
    async def handler(request: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "result": {"echo": request.get("params", {})},
            "id": request.get("id"),
        }

    t = HTTPTransport(handler=handler, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {},
        }
    ).encode("utf-8")

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"POST / HTTP/1.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    await writer.drain()

    response_line = await reader.readline()
    assert b"200 OK" in response_line

    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        text = line.decode().strip()
        if not text:
            break
        if ":" in text:
            k, v = text.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    content_length = int(headers.get("content-length", "0"))
    resp_body = await reader.read(content_length)
    resp = json.loads(resp_body.decode())
    assert resp["id"] == 1
    assert resp["result"] == {"echo": {}}

    writer.close()
    await writer.wait_closed()
    await t.stop()


# ── MCPServer ──


def test_mcp_server_init() -> None:
    server = MCPServer()
    assert server.config is not None
    assert server.registry is not None
    assert server.handler is not None
    assert server._running is False


def test_mcp_server_with_config() -> None:
    config = EcosystemConfig(mcp_transport="sse", mcp_port=9000)
    server = MCPServer(config=config)
    assert server.config.mcp_transport == "sse"
    assert server.config.mcp_port == 9000


async def test_mcp_server_register_builtin() -> None:
    server = MCPServer()
    server.registry.register_builtin()
    assert server.registry.get("caveman_compress") is not None


async def test_mcp_server_handler_ping() -> None:
    server = MCPServer()
    server.registry.register_builtin()
    resp = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
        }
    )
    assert resp["result"] == {}


async def test_mcp_server_handler_tools_list() -> None:
    server = MCPServer()
    server.registry.register_builtin()
    resp = await server.handler.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
    )
    assert "tools" in resp["result"]
