"""MCP 传输层。

支持三种传输模式：
- StdioTransport: stdin/stdout JSON-RPC，用于 Claude Desktop 子进程
- SSETransport: HTTP + Server-Sent Events，用于远程客户端
- HTTPTransport: Streamable HTTP (MCP 2026-07-28)

每个传输实例绑定一个 MCPHandler 处理 JSON-RPC 请求。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Transport(ABC):
    """MCP 传输抽象基类。"""

    def __init__(self, handler: Callable[[dict], Any] | None = None) -> None:
        self._handler = handler

    def set_handler(self, handler: Callable[[dict], Any]) -> None:
        self._handler = handler

    @abstractmethod
    async def start(self) -> None:
        """启动传输层。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止传输层。"""

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None:
        """发送消息。"""


class StdioTransport(Transport):
    """stdin/stdout 传输，用于 Claude Desktop 子进程模式。

    协议格式：每行一个 JSON-RPC 消息，以 \\n 分隔。
    """

    def __init__(self, handler: Callable[[dict], Any] | None = None) -> None:
        super().__init__(handler)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = False
        self._read_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            logger.warning("StdioTransport already running")
            return
        self._running = True
        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        self._writer = asyncio.StreamWriter(
            write_transport, write_protocol, self._reader, loop
        )
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("StdioTransport started")

    async def stop(self) -> None:
        self._running = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.info("StdioTransport stopped")

    async def send(self, message: dict[str, Any]) -> None:
        if not self._writer:
            logger.error("StdioTransport send: writer not initialized")
            return
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except Exception as e:
            logger.error("StdioTransport send error: %s", e)

    async def _read_loop(self) -> None:
        if not self._reader:
            return
        while self._running:
            try:
                line = await self._reader.readline()
                if not line:
                    logger.info("StdioTransport: EOF received")
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    request = json.loads(text)
                except json.JSONDecodeError as e:
                    logger.warning("StdioTransport: invalid JSON: %s", e)
                    await self.send({
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error"},
                        "id": None,
                    })
                    continue
                if self._handler:
                    try:
                        response = await self._handler(request)
                        if response is not None:
                            await self.send(response)
                    except Exception as e:
                        logger.error("StdioTransport handler error: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("StdioTransport read error: %s", e)
                break


class SSETransport(Transport):
    """HTTP + Server-Sent Events 传输。

    客户端通过 HTTP POST 发送 JSON-RPC 请求，
    服务端通过 SSE 事件流返回响应。
    """

    def __init__(
        self,
        handler: Callable[[dict], Any] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        super().__init__(handler)
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._running = False
        self._sessions: dict[str, asyncio.Queue] = {}

    @property
    def port(self) -> int:
        if self._server:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        actual_port = self.port
        logger.info("SSETransport started on %s:%d", self._host, actual_port)

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for q in self._sessions.values():
            await q.put(None)
        self._sessions.clear()
        logger.info("SSETransport stopped")

    async def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False)
        for session_id, queue in list(self._sessions.items()):
            await queue.put(f"data: {data}\n\n")

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        queue = self._sessions.get(session_id)
        if queue is None:
            return
        data = json.dumps(message, ensure_ascii=False)
        await queue.put(f"data: {data}\n\n")

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            request_text = request_line.decode("utf-8").strip()
            headers: dict[str, str] = {}
            while True:
                header_line = await reader.readline()
                header_text = header_line.decode("utf-8").strip()
                if not header_text:
                    break
                if ":" in header_text:
                    key, value = header_text.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            content_length = int(headers.get("content-length", "0"))

            if request_text.startswith("GET"):
                await self._handle_sse_handshake(writer)
                return

            if request_text.startswith("POST") and content_length > 0:
                body = await reader.read(content_length)
                request = json.loads(body.decode("utf-8"))
                if self._handler:
                    response = await self._handler(request)
                    if response is not None:
                        resp_body = json.dumps(response, ensure_ascii=False)
                        resp_bytes = resp_body.encode("utf-8")
                        writer.write(
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: " + str(len(resp_bytes)).encode() + b"\r\n"
                            b"\r\n" + resp_bytes
                        )
                        await writer.drain()
                writer.close()
                await writer.wait_closed()
        except Exception as e:
            logger.error("SSETransport connection error: %s", e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_sse_handshake(self, writer: asyncio.StreamWriter) -> None:
        import uuid

        session_id = uuid.uuid4().hex[:12]
        queue: asyncio.Queue = asyncio.Queue()
        self._sessions[session_id] = queue

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()

        try:
            while self._running:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                if data is None:
                    break
                writer.write(data.encode("utf-8"))
                await writer.drain()
        except asyncio.TimeoutError:
            writer.write(b": keepalive\n\n")
            await writer.drain()
        except Exception as e:
            logger.debug("SSE session %s ended: %s", session_id, e)
        finally:
            self._sessions.pop(session_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


class HTTPTransport(Transport):
    """Streamable HTTP 传输（MCP 2026-07-28）。

    单次 HTTP POST 请求/响应，支持批量操作。
    """

    def __init__(
        self,
        handler: Callable[[dict], Any] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        super().__init__(handler)
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._running = False

    @property
    def port(self) -> int:
        if self._server:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_request, self._host, self._port
        )
        actual_port = self.port
        logger.info("HTTPTransport started on %s:%d", self._host, actual_port)

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("HTTPTransport stopped")

    async def send(self, message: dict[str, Any]) -> None:
        logger.debug("HTTPTransport send: direct push not supported")

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            request_text = request_line.decode("utf-8").strip()
            headers: dict[str, str] = {}
            while True:
                header_line = await reader.readline()
                header_text = header_line.decode("utf-8").strip()
                if not header_text:
                    break
                if ":" in header_text:
                    key, value = header_text.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            content_length = int(headers.get("content-length", "0"))

            if not request_text.startswith("POST") or content_length == 0:
                resp = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                writer.write(resp)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            body = await reader.read(content_length)
            request = json.loads(body.decode("utf-8"))

            if self._handler:
                response = await self._handler(request)
                if response is not None:
                    resp_body = json.dumps(response, ensure_ascii=False)
                    resp_bytes = resp_body.encode("utf-8")
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(resp_bytes)).encode() + b"\r\n"
                        b"\r\n" + resp_bytes
                    )
                    await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception as e:
            logger.error("HTTPTransport request error: %s", e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def create_transport(
    transport_type: str,
    handler: Callable[[dict], Any] | None = None,
    **kwargs: Any,
) -> Transport:
    """工厂方法：根据类型创建传输实例。"""
    if transport_type == "stdio":
        return StdioTransport(handler=handler)
    elif transport_type == "sse":
        return SSETransport(
            handler=handler,
            host=kwargs.get("host", "127.0.0.1"),
            port=kwargs.get("port", 0),
        )
    elif transport_type == "http":
        return HTTPTransport(
            handler=handler,
            host=kwargs.get("host", "127.0.0.1"),
            port=kwargs.get("port", 0),
        )
    else:
        raise ValueError(f"Unknown transport type: {transport_type}")
