# MCP transport layer.
#
# Supports three transport modes:
# - StdioTransport: stdin/stdout JSON-RPC, for the Claude Desktop subprocess
# - SSETransport: HTTP + Server-Sent Events, for remote clients
# - HTTPTransport: Streamable HTTP (MCP 2026-07-28)
#
# Each transport instance binds one MCPHandler to process JSON-RPC requests.

from __future__ import annotations

import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from fusion_plugins_ecosystem.logging_setup import (
    reset_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger(__name__)


def _stamp_cid(request: dict[str, Any]) -> Any:
    # Take or generate the correlation_id from the request's _meta.cid and
    # stamp it into the current context. Returns the token.
    cid = None
    if isinstance(request, dict):
        meta = request.get("_meta")
        if isinstance(meta, dict):
            cid = meta.get("cid") or meta.get("correlation_id")
    if not cid:
        cid = f"{id(request) & 0xFFFFFFFF:08x}-{int(asyncio.get_running_loop().time()) & 0xFFFF:04x}"
    return set_correlation_id(str(cid))


# Hard cap on request body size (bytes), prevents a forged Content-Length
# from causing OOM
_MAX_BODY = 1 << 20  # 1 MiB
# Max length of a single header line (bytes)
_MAX_HEADER_LINE = 8192
# Max number of header lines
_MAX_HEADERS = 100
# Connection read timeout (seconds): guards request line/headers/body stages
# against slow attacks
_READ_TIMEOUT = 30.0
# P3-6: capacity cap for a single connection's SSE event queue. Slow clients
# no longer accumulate unbounded memory; when the queue is full,
# send_to_session drops the new event and logs (without blocking the RPC
# caller).
_SSE_QUEUE_MAXSIZE = 256


class Transport(ABC):
    # MCP transport abstract base class.

    def __init__(self, handler: Callable[[dict], Any] | None = None) -> None:
        self._handler = handler

    def set_handler(self, handler: Callable[[dict], Any]) -> None:
        self._handler = handler

    @abstractmethod
    async def start(self) -> None:
        # Start the transport.
        ...

    @abstractmethod
    async def stop(self) -> None:
        # Stop the transport.
        ...

    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None:
        # Send a message.
        ...


class StdioTransport(Transport):
    # stdin/stdout transport, for the Claude Desktop subprocess mode.
    #
    # Protocol format: one JSON-RPC message per line, separated by \n.

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
                    await self.send(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32700, "message": "Parse error"},
                            "id": None,
                        }
                    )
                    continue
                if self._handler:
                    token = _stamp_cid(request)
                    try:
                        response = await self._handler(request)
                        if response is not None:
                            await self.send(response)
                    except Exception as e:
                        logger.error("StdioTransport handler error: %s", e)
                    finally:
                        reset_correlation_id(token)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("StdioTransport read error: %s", e)
                break


class SSETransport(Transport):
    # HTTP + Server-Sent Events transport.
    #
    # Clients send JSON-RPC requests via HTTP POST; the server returns
    # responses over the SSE event stream.

    def __init__(
        self,
        handler: Callable[[dict], Any] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        auth_token: str | None = None,
        metrics_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(handler)
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._metrics_provider = metrics_provider
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
        # Broadcasting is disabled: JSON-RPC responses are per-request, and
        # the POST branch already writes back synchronously. Cross-client
        # broadcast would leak data (P0-2). Server-initiated notifications
        # use send_to_session.
        logger.warning(
            "SSETransport: send() broadcast is disabled, use send_to_session to route per session"
        )

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        queue = self._sessions.get(session_id)
        if queue is None:
            logger.debug("SSETransport: session %s not found, dropping message", session_id)
            return
        data = json.dumps(message, ensure_ascii=False)
        # P3-6: drop on full queue instead of blocking. When a slow client
        # cannot keep up, put_nowait raises QueueFull; drop the event and
        # log, avoiding a hung send() caller plus unbounded memory growth.
        try:
            queue.put_nowait(f"data: {data}\n\n")
        except asyncio.QueueFull:
            logger.warning(
                "SSETransport: session %s event queue full (>%d), dropping event",
                session_id,
                _SSE_QUEUE_MAXSIZE,
            )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_text, headers = await _read_http_head(reader)
            if request_text is None:
                writer.close()
                await writer.wait_closed()
                return

            # P2-8: unauthenticated health-probe short circuit
            if _is_health_request(request_text):
                await _write_health_response(writer)
                writer.close()
                await writer.wait_closed()
                return

            # Observability: unauthenticated GET /metrics short circuit
            # (Prometheus scrape)
            if _is_metrics_request(request_text) and self._metrics_provider:
                await _write_metrics_response(writer, self._metrics_provider())
                writer.close()
                await writer.wait_closed()
                return

            content_length = _safe_content_length(headers)

            # R6: SSE/HTTP remote transport auth; pass only when the Bearer
            # token matches
            if not _check_auth(self._auth_token, headers):
                logger.warning("SSETransport: auth failed, rejecting request")
                await _write_simple_response(writer, 401, b"Unauthorized")
                writer.close()
                await writer.wait_closed()
                return

            if request_text.startswith("GET"):
                # issue #62: SSE handshake only on the /sse path; unknown
                # paths fail fast with 404 instead of 200-then-hang (hanging
                # fools health probes and masks upstream misrouting).
                if _request_path(request_text).rstrip("/") == "/sse":
                    await self._handle_sse_handshake(writer)
                else:
                    logger.warning(
                        "SSETransport: GET %s is not an SSE endpoint, returning 404",
                        _request_path(request_text),
                    )
                    await _write_simple_response(writer, 404, b"Not Found")
                    writer.close()
                    await writer.wait_closed()
                return

            if request_text.startswith("POST"):
                if content_length <= 0 or content_length > _MAX_BODY:
                    await _write_simple_response(writer, 400, b"Bad Request")
                    writer.close()
                    await writer.wait_closed()
                    return
                body = await asyncio.wait_for(
                    reader.read(content_length), timeout=_READ_TIMEOUT
                )
                try:
                    request = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    logger.warning("SSETransport: invalid JSON body: %s", e)
                    await _write_simple_response(writer, 400, b"Bad Request")
                    writer.close()
                    await writer.wait_closed()
                    return
                if self._handler:
                    token = _stamp_cid(request)
                    try:
                        response = await self._handler(request)
                    finally:
                        reset_correlation_id(token)
                    if response is not None:
                        resp_body = json.dumps(response, ensure_ascii=False)
                        resp_bytes = resp_body.encode("utf-8")
                        writer.write(
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: "
                            + str(len(resp_bytes)).encode()
                            + b"\r\n"
                            b"\r\n" + resp_bytes
                        )
                        await writer.drain()
            else:
                await _write_simple_response(writer, 400, b"Bad Request")
            writer.close()
            await writer.wait_closed()
        except TimeoutError:
            logger.warning("SSETransport: connection read timed out")
            try:
                await _write_simple_response(writer, 408, b"Request Timeout")
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
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
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
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
        except TimeoutError:
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
    # Streamable HTTP transport (MCP 2026-07-28).
    #
    # One HTTP POST request/response per call; supports batch operations.

    def __init__(
        self,
        handler: Callable[[dict], Any] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        auth_token: str | None = None,
        metrics_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(handler)
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._metrics_provider = metrics_provider
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
            request_text, headers = await _read_http_head(reader)
            if request_text is None:
                writer.close()
                await writer.wait_closed()
                return

            # P2-8: unauthenticated health-probe short circuit
            if _is_health_request(request_text):
                await _write_health_response(writer)
                writer.close()
                await writer.wait_closed()
                return

            # Observability: unauthenticated GET /metrics short circuit
            # (Prometheus scrape)
            if _is_metrics_request(request_text) and self._metrics_provider:
                await _write_metrics_response(writer, self._metrics_provider())
                writer.close()
                await writer.wait_closed()
                return

            content_length = _safe_content_length(headers)

            # R6: SSE/HTTP remote transport auth; pass only when the Bearer
            # token matches
            if not _check_auth(self._auth_token, headers):
                logger.warning("HTTPTransport: auth failed, rejecting request")
                await _write_simple_response(writer, 401, b"Unauthorized")
                writer.close()
                await writer.wait_closed()
                return

            if not request_text.startswith("POST"):
                # issue #62: the JSON-RPC endpoint accepts POST only; unknown
                # paths/methods fail fast with 404 (the previous 400 masked
                # misrouting — 200/4xx can both be judged "alive").
                logger.warning(
                    "HTTPTransport: %s is not a JSON-RPC POST endpoint, returning 404",
                    _request_path(request_text),
                )
                await _write_simple_response(writer, 404, b"Not Found")
                writer.close()
                await writer.wait_closed()
                return
            if content_length <= 0 or content_length > _MAX_BODY:
                await _write_simple_response(
                    writer,
                    413 if content_length > _MAX_BODY else 400,
                    b"Bad Request" if content_length <= 0 else b"Payload Too Large",
                )
                writer.close()
                await writer.wait_closed()
                return

            body = await asyncio.wait_for(
                reader.read(content_length), timeout=_READ_TIMEOUT
            )
            try:
                request = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.warning("HTTPTransport: invalid JSON body: %s", e)
                await _write_simple_response(writer, 400, b"Bad Request")
                writer.close()
                await writer.wait_closed()
                return

            if self._handler:
                token = _stamp_cid(request)
                try:
                    response = await self._handler(request)
                finally:
                    reset_correlation_id(token)
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
        except TimeoutError:
            logger.warning("HTTPTransport: connection read timed out")
            try:
                await _write_simple_response(writer, 408, b"Request Timeout")
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        except Exception as e:
            logger.error("HTTPTransport request error: %s", e)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def _read_http_head(
    reader: asyncio.StreamReader,
) -> tuple[str | None, dict[str, str]]:
    # Read the HTTP request line + headers with a timeout and caps on line
    # count/length.
    #
    # Returns (request_text, headers); on EOF of the request line returns
    # (None, {}).
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
    except TimeoutError:
        raise
    if not request_line:
        return None, {}
    request_text = request_line.decode("utf-8", errors="replace").strip()
    headers: dict[str, str] = {}
    for _ in range(_MAX_HEADERS):
        try:
            header_line = await asyncio.wait_for(
                reader.readline(), timeout=_READ_TIMEOUT
            )
        except TimeoutError:
            raise
        if len(header_line) > _MAX_HEADER_LINE:
            logger.warning("transport: header line too long, rejecting request")
            return None, {}
        header_text = header_line.decode("utf-8", errors="replace").strip()
        if not header_text:
            break
        if ":" in header_text:
            key, value = header_text.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    else:
        logger.warning("transport: header line count exceeds %d, rejecting request", _MAX_HEADERS)
        return None, {}
    return request_text, headers


def _safe_content_length(headers: dict[str, str]) -> int:
    # Safely parse Content-Length; returns -1 on invalid/missing.
    raw = headers.get("content-length")
    if raw is None:
        return -1
    try:
        cl = int(raw)
    except (TypeError, ValueError):
        return -1
    if cl < 0:
        return -1
    return cl


def _check_auth(auth_token: str | None, headers: dict[str, str]) -> bool:
    # R6: validate Authorization: Bearer <token>.
    #
    # auth_token=None means auth is disabled (local/test), allow. When
    # enabled, the token must match; hmac.compare_digest constant-time
    # comparison guards against side channels.
    if not auth_token:
        return True
    import hmac

    raw = headers.get("authorization", "")
    if not raw.startswith("Bearer "):
        return False
    return hmac.compare_digest(raw[len("Bearer ") :], auth_token)


def _request_path(request_text: str) -> str:
    # Extract the path from the request line (issue #62: SSE/HTTP transports
    # dispatch by path).
    parts = request_text.split()
    return parts[1] if len(parts) >= 2 else "/"


async def _write_simple_response(
    writer: asyncio.StreamWriter, status: int, message: bytes
) -> None:
    # Write a simple HTTP error response with no body.
    reason = {
        400: b"Bad Request",
        401: b"Unauthorized",
        404: b"Not Found",
        405: b"Method Not Allowed",
        408: b"Request Timeout",
        413: b"Payload Too Large",
    }.get(status, b"Error")
    resp = (
        f"HTTP/1.1 {status} ".encode() + reason + b"\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(message)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + message
    )
    writer.write(resp)
    await writer.drain()


def _is_health_request(request_text: str) -> bool:
    # P2-8: whether this is an unauthenticated GET /health probe request.
    parts = request_text.split()
    if len(parts) < 2 or parts[0] != "GET":
        return False
    return parts[1].rstrip("/") == "/health"


async def _write_health_response(writer: asyncio.StreamWriter) -> None:
    # Write the unauthenticated health probe response (for orchestrators /
    # K8s liveness checks).
    body = b'{"status":"ok"}'
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    await writer.drain()


def _is_metrics_request(request_text: str) -> bool:
    # Whether this is an unauthenticated GET /metrics request (Prometheus
    # scrape).
    parts = request_text.split()
    if len(parts) < 2 or parts[0] != "GET":
        return False
    return parts[1].rstrip("/") == "/metrics"


async def _write_metrics_response(
    writer: asyncio.StreamWriter, exposition: str
) -> None:
    # Write the Prometheus text exposition format response (unauthenticated,
    # for scrape pulls).
    body = exposition.encode("utf-8")
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain; version=0.0.4\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    await writer.drain()


def create_transport(
    transport_type: str,
    handler: Callable[[dict], Any] | None = None,
    **kwargs: Any,
) -> Transport:
    # Factory: create a transport instance by type.
    if transport_type == "stdio":
        return StdioTransport(handler=handler)
    elif transport_type == "sse":
        return SSETransport(
            handler=handler,
            host=kwargs.get("host", "127.0.0.1"),
            port=kwargs.get("port", 0),
            auth_token=kwargs.get("auth_token"),
            metrics_provider=kwargs.get("metrics_provider"),
        )
    elif transport_type == "http":
        return HTTPTransport(
            handler=handler,
            host=kwargs.get("host", "127.0.0.1"),
            port=kwargs.get("port", 0),
            auth_token=kwargs.get("auth_token"),
            metrics_provider=kwargs.get("metrics_provider"),
        )
    else:
        raise ValueError(f"Unknown transport type: {transport_type}")
