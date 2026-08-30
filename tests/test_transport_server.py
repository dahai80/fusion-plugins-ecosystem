"""MCP 传输层 + Server 测试。"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem import server as server_module
from fusion_plugins_ecosystem import __version__
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


async def test_sse_transport_post_round_trip() -> None:
    """真实回环：SSETransport 处理 HTTP POST 请求并返回 JSON-RPC 响应。"""

    async def handler(request: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "result": {"method": request.get("method")},
            "id": request.get("id"),
        }

    t = SSETransport(handler=handler, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    body = json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
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
    resp = json.loads((await reader.read(content_length)).decode())
    assert resp["id"] == 7
    assert resp["result"] == {"method": "ping"}

    writer.close()
    await writer.wait_closed()
    await t.stop()


async def test_sse_transport_get_handshake() -> None:
    """真实回环：SSETransport 处理 GET 请求，建立 SSE 事件流。"""
    t = SSETransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"200 OK" in response_line

    header_bytes = b""
    while True:
        line = await reader.readline()
        header_bytes += line
        if line in (b"\r\n", b"\n", b""):
            break
    assert b"text/event-stream" in header_bytes

    writer.close()
    await writer.wait_closed()
    await t.stop()


async def test_http_transport_bad_request() -> None:
    """真实回环：HTTPTransport 对非 POST/无 body 请求返回 400。"""
    t = HTTPTransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"400 Bad Request" in response_line

    writer.close()
    await writer.wait_closed()
    await t.stop()


async def test_http_transport_bad_request_closes_connection() -> None:
    """400 错误路径必须关闭连接（HTTP/Connection: close），否则客户端
    阻塞读 EOF 致测试超时挂起。回归 HTTPTransport GET 400 漏 close。"""
    t = HTTPTransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"400 Bad Request" in response_line
    # read(-1) 仅在 EOF 返回；服务端写完 400 后主动关连接，
    # 故 wait_for 内必返回（证明连接已关闭，非永久挂起泄漏）。
    rest = await asyncio.wait_for(reader.read(-1), timeout=5)
    assert b"Connection: close" in rest

    writer.close()
    await writer.wait_closed()
    await t.stop()


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


async def test_mcp_server_start_stop_sse() -> None:
    """真实启动/停止生命周期：SSE transport + register_builtin + keep-alive + stop。

    关键断言：start() 必须阻塞不返回（否则 asyncio.run 结束、CLI 闪退）。
    通过检查 start_task 仍 pending 确保服务真正存活。
    """
    server = MCPServer(config=EcosystemConfig(mcp_transport="sse", mcp_port=0))
    start_task = asyncio.create_task(server.start(transport="sse"))
    await asyncio.sleep(0.3)
    assert server._running is True
    assert server.transport is not None
    assert server.registry.get("caveman_compress") is not None
    # keep-alive：start() 未返回，任务仍 pending（非 done）
    assert not start_task.done(), "start() 提前返回，服务未保活"
    assert server.registry.get("caveman_compress").version == __version__

    await server.stop()
    assert server._running is False
    await start_task


async def test_mcp_server_start_already_running_noop() -> None:
    """二次 start 应幂等返回，不重复注册/启动。

    start() 现已阻塞保活（keep-alive），故首次 start 须作为后台任务，
    否则 await 直接死等 stop_event。
    """
    server = MCPServer(config=EcosystemConfig(mcp_transport="sse", mcp_port=0))
    start_task = asyncio.create_task(server.start(transport="sse"))
    await asyncio.sleep(0.3)
    assert server._running is True
    builtin_count_before = len(server.registry.list())
    # 二次 start：_running 已 True → 幂等返回，不阻塞
    await asyncio.wait_for(server.start(transport="sse"), timeout=2)
    builtin_count_after = len(server.registry.list())
    assert builtin_count_before == builtin_count_after
    await server.stop()
    await asyncio.wait_for(start_task, timeout=5)


async def test_mcp_server_stop_when_not_running_noop() -> None:
    """未启动时 stop 应安全返回。"""
    server = MCPServer()
    await server.stop()
    assert server._running is False


async def test_mcp_server_stdio_signal_stop(monkeypatch) -> None:
    """stdio 路径：模拟信号处理器触发 stop_event，验证 start 正常退出。

    实例级 patch loop.add_signal_handler：SelectorEventLoop 自身实现了
    add_signal_handler 会遮蔽 AbstractEventLoop 上的类级 patch，故必须
    patch 运行中的 loop 实例。
    """
    server = MCPServer(config=EcosystemConfig(mcp_transport="stdio"))

    class _FakeTransport:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "fusion_plugins_ecosystem.server.create_transport",
        lambda *a, **kw: _FakeTransport(),
    )

    loop = asyncio.get_running_loop()

    def fake_add_signal(sig, callback, *args):
        loop.call_later(0.05, callback)

    monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal)

    await asyncio.wait_for(server.start(transport="stdio"), timeout=5)
    assert server._running is False
    assert server._transport is not None


def test_main_cli_sse_start_stop(monkeypatch, tmp_path) -> None:
    """main() CLI：解析参数 → 构造 MCPServer → 启动 sse → 自动停止。"""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fusion-plugin-server",
            "--transport",
            "sse",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--log-level",
            "ERROR",
        ],
    )

    stopped = asyncio.Event()

    async def fake_start(self, transport="stdio", **kwargs):
        self._running = True
        await asyncio.sleep(0.1)
        await self.stop()
        stopped.set()

    monkeypatch.setattr(MCPServer, "start", fake_start)

    server_module.main()
    assert stopped.is_set()


def test_main_cli_keyboard_interrupt(monkeypatch) -> None:
    """main() CLI：KeyboardInterrupt 被吞掉，正常退出（覆盖 except 分支）。"""
    monkeypatch.setattr(
        sys,
        "argv",
        ["fusion-plugin-server", "--transport", "stdio"],
    )

    async def raise_kbi(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(MCPServer, "start", raise_kbi)

    server_module.main()
