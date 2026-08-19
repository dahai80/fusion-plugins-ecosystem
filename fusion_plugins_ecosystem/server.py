"""MCP Server 入口。

组合传输层 + JSON-RPC 处理器 + 运行时，
提供 fusion-plugin-server 命令入口。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import MCPHandler
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import PluginRegistry
from fusion_plugins_ecosystem.token_meter import TokenMeter
from fusion_plugins_ecosystem.transport import create_transport

logger = logging.getLogger(__name__)


class MCPServer:
    """MCP Server 入口，组合传输 + 处理器 + 运行时。

    用法：
        server = MCPServer()
        await server.start(transport="stdio")

    或通过 CLI：
        fusion-plugin-server --transport stdio
        fusion-plugin-server --transport sse --port 8765
    """

    def __init__(self, config: EcosystemConfig | None = None) -> None:
        self.config = config or EcosystemConfig()
        self.desk = DeskRuntime(
            mcp_gateway_port=self.config.mcp_port or None,
        )
        self.registry = PluginRegistry(desk=self.desk)
        self.lifecycle = PluginLifecycle(self.registry)
        self.token_meter = TokenMeter(self.desk)
        self.handler = MCPHandler(
            registry=self.registry,
            lifecycle=self.lifecycle,
            desk=self.desk,
            config=self.config,
            token_meter=self.token_meter,
        )
        self._transport: Any = None
        self._running = False
        # 停止信号：start() 等待、stop() 触发，跨协程保活/停机
        self._stop_event: asyncio.Event | None = None

    async def start(
        self,
        transport: str = "stdio",
        **kwargs: Any,
    ) -> None:
        """启动 MCP Server。"""
        if self._running:
            logger.warning("MCPServer already running")
            return

        self.registry.register_builtin()

        transport_type = transport or self.config.mcp_transport
        host = kwargs.get("host", self.config.mcp_host)
        port = kwargs.get("port", self.config.mcp_port)

        self._transport = create_transport(
            transport_type,
            handler=self.handler.handle,
            host=host,
            port=port,
        )

        self._running = True
        logger.info(
            "MCPServer starting: transport=%s host=%s port=%s",
            transport_type,
            host,
            port,
        )

        await self._transport.start()

        # 所有传输类型都需阻塞至停止信号，否则 start() 立即返回会导致
        # asyncio.run 结束、进程退出（sse/http 此前无 keep-alive → CLI 闪退）
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # 部分平台不支持 add_signal_handler，退化轮询
                pass
        await self._stop_event.wait()
        # 唤醒后停传输；若 stop() 已先行停掉则 _transport_stop 幂等返回
        await self._transport_stop()

    async def _transport_stop(self) -> None:
        """幂等停止传输：_running 守卫防止 start()/stop() 双重 stop。"""
        if not self._running:
            return
        self._running = False
        if self._transport:
            await self._transport.stop()
        logger.info("MCPServer stopped")

    async def stop(self) -> None:
        """停止 MCP Server：触发 stop_event 解除 start() 阻塞，并停传输。"""
        if self._stop_event is not None:
            self._stop_event.set()
        await self._transport_stop()

    @property
    def transport(self) -> Any:
        return self._transport


def main() -> None:
    """CLI 入口：fusion-plugin-server 命令。"""
    parser = argparse.ArgumentParser(
        prog="fusion-plugin-server",
        description="Fusion Plugins Ecosystem MCP Server",
    )
    parser.add_argument(
        "--transport",
        "-t",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE/HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=0,
        help="Port for SSE/HTTP transport (0=auto)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    server = MCPServer()
    try:
        asyncio.run(
            server.start(
                transport=args.transport,
                host=args.host,
                port=args.port,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
