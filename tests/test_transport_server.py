"""MCP transport layer + server tests."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from fusion_plugins_ecosystem import __version__
from fusion_plugins_ecosystem import server as server_module
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.server import MCPServer
from fusion_plugins_ecosystem.transport import (
    HTTPTransport,
    SSETransport,
    StdioTransport,
    create_transport,
)

# ── create_transport factory ──


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


# ── StdioTransport basics ──


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


async def test_sse_send_to_session_drops_when_queue_full() -> None:
    """P3-6b: drop events on a full queue instead of blocking. Once the queue
    is full, send_to_session neither hangs nor raises."""
    from fusion_plugins_ecosystem.transport import _SSE_QUEUE_MAXSIZE

    # The production queue cap exists and is positive
    assert _SSE_QUEUE_MAXSIZE > 0

    t = SSETransport()
    # Directly build a session with a full queue (bypasses the handshake,
    # isolating the queue-boundary behavior)
    full_q: asyncio.Queue = asyncio.Queue(maxsize=2)
    t._sessions["full"] = full_q
    full_q.put_nowait("data: a\n\n")
    full_q.put_nowait("data: b\n\n")
    assert full_q.full()
    # An over-cap send must return immediately (dropped), not block
    await asyncio.wait_for(t.send_to_session("full", {"x": 1}), timeout=2.0)
    # Queue still has 2 items (the new event was dropped)
    assert full_q.qsize() == 2

    # Tiny-maxsize boundary: a single item fills it
    one_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    t._sessions["one"] = one_q
    await t.send_to_session("one", {"y": 1})
    assert one_q.qsize() == 1
    await t.send_to_session("one", {"y": 2})  # dropped once full
    assert one_q.qsize() == 1


# ── MCPServer ──


async def test_sse_transport_post_round_trip() -> None:
    """Real round trip: SSETransport handles an HTTP POST and returns a JSON-RPC response."""

    async def handler(request: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "result": {"method": request.get("method")},
            "id": request.get("id"),
        }

    t = SSETransport(handler=handler, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}).encode("utf-8")

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
    """Real round trip: SSETransport handles a GET request and establishes an SSE event stream."""
    t = SSETransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /sse HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
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


async def test_sse_transport_get_unknown_path_404() -> None:
    """issue #62: SSETransport returns 404 fast-fail for GETs to non-/sse paths,
    no longer 200-then-hang (a hang fools health probes and masks upstream misrouting)."""
    t = SSETransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"404 Not Found" in response_line
    rest = await asyncio.wait_for(reader.read(-1), timeout=5)
    assert b"Connection: close" in rest

    writer.close()
    await writer.wait_closed()
    await t.stop()


async def test_http_transport_bad_request() -> None:
    """Real round trip: HTTPTransport returns 404 for non-POST/bodyless requests (issue #62)."""
    t = HTTPTransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"404 Not Found" in response_line

    writer.close()
    await writer.wait_closed()
    await t.stop()


async def test_http_transport_bad_request_closes_connection() -> None:
    """The 404 error path must close the connection (HTTP/Connection: close), otherwise the
    client blocks reading for EOF and the test times out. Regression guard for HTTPTransport's
    non-POST missing close (issue #62)."""
    t = HTTPTransport(handler=None, host="127.0.0.1", port=0)
    await t.start()
    port = t.port

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()

    response_line = await reader.readline()
    assert b"404 Not Found" in response_line
    # read(-1) only returns at EOF; the server closes the connection itself
    # after writing the 400, so wait_for must return (proving the connection
    # closed, not a permanent hang/leak).
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
    """Real start/stop lifecycle: SSE transport + register_builtin + keep-alive + stop.

    Key assertion: start() must block and not return (otherwise asyncio.run ends
    and the CLI exits immediately). Checking that start_task is still pending
    ensures the server is genuinely alive.
    """
    server = MCPServer(config=EcosystemConfig(mcp_transport="sse", mcp_port=0))
    start_task = asyncio.create_task(server.start(transport="sse"))
    await asyncio.sleep(0.3)
    assert server._running is True
    assert server.transport is not None
    assert server.registry.get("caveman_compress") is not None
    # keep-alive: start() has not returned, the task is still pending (not done)
    assert not start_task.done(), "start() returned early, server not kept alive"
    assert server.registry.get("caveman_compress").version == __version__

    await server.stop()
    assert server._running is False
    await start_task


async def test_mcp_server_start_already_running_noop() -> None:
    """A second start should return idempotently without re-registering/re-starting.

    start() now blocks to keep the server alive (keep-alive), so the first start
    must run as a background task, otherwise await dead-waits on stop_event.
    """
    server = MCPServer(config=EcosystemConfig(mcp_transport="sse", mcp_port=0))
    start_task = asyncio.create_task(server.start(transport="sse"))
    await asyncio.sleep(0.3)
    assert server._running is True
    builtin_count_before = len(server.registry.list())
    # Second start: _running is already True -> idempotent return, no blocking
    await asyncio.wait_for(server.start(transport="sse"), timeout=2)
    builtin_count_after = len(server.registry.list())
    assert builtin_count_before == builtin_count_after
    await server.stop()
    await asyncio.wait_for(start_task, timeout=5)


async def test_mcp_server_stop_when_not_running_noop() -> None:
    """stop should return safely when not started."""
    server = MCPServer()
    await server.stop()
    assert server._running is False


async def test_mcp_server_stdio_signal_stop(monkeypatch) -> None:
    """stdio path: simulate a signal handler triggering stop_event and verify start exits cleanly.

    Instance-level patch of loop.add_signal_handler: SelectorEventLoop implements
    its own add_signal_handler, which shadows a class-level patch on
    AbstractEventLoop, so the running loop instance must be patched.
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
    """main() CLI: parse args -> build MCPServer -> start sse -> auto-stop."""
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
    """main() CLI: KeyboardInterrupt is swallowed and exit is clean (covers the except branch)."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["fusion-plugin-server", "--transport", "stdio"],
    )

    async def raise_kbi(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(MCPServer, "start", raise_kbi)

    server_module.main()
