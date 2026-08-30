"""MCP Server 入口。

组合传输层 + JSON-RPC 处理器 + 运行时，
提供 fusion-plugin-server 命令入口。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import ipaddress
import logging
import os
import signal
import socket
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


def _is_loopback_host(host: str | None) -> bool:
    """判断 host 是否为 loopback（127.0.0.0/8、::1、localhost）。

    P0-1：远程传输（sse/http）在非 loopback 绑定时强制鉴权，
    loopback 默认放行（本地单机/测试场景）。
    """
    if not host:
        return True
    if host in ("localhost", "::1"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        # 非 IP 字面量（如主机名）默认视为非 loopback，保守拒绝
        return False


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
        # A3：vram_limit_mb 同步到 DeskRuntime 显存总预算
        if self.config.vram_limit_mb:
            self.desk.vram_total_mb = self.config.vram_limit_mb
        self.registry = PluginRegistry(desk=self.desk)
        # A2：lifecycle 注入 config，超时/重启/心跳阈值改由配置驱动
        self.lifecycle = PluginLifecycle(self.registry, config=self.config)
        # A5：token_meter 注入配置的计量阈值与持久化路径
        self.token_meter = TokenMeter(
            self.desk,
            max_records=self.config.max_token_records,
            persist_path=self.config.token_persist_path,
        )
        self.handler = MCPHandler(
            registry=self.registry,
            lifecycle=self.lifecycle,
            desk=self.desk,
            config=self.config,
            token_meter=self.token_meter,
        )
        # A4：启动时从 config_center 恢复持久化配置与已安装插件集合
        self.handler.restore_config()
        # A3：应用配置日志级别到根 logger（CLI --log-level 优先，此处补可编程入口）
        if self.config.log_level and self.config.log_level != "INFO":
            logging.getLogger().setLevel(
                getattr(logging, self.config.log_level, logging.INFO)
            )
        self._transport: Any = None
        self._running = False
        # 停止信号：start() 等待、stop() 触发，跨协程保活/停机
        self._stop_event: asyncio.Event | None = None
        # P1-4：atexit 注册幂等守卫
        self._atexit_registered = False

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

        # 默认挂载：default_mount_compressor 开启时，自动 load+enable
        # 所有 default_mounted 插件，使 tools/call 可直接调用（无需额外 install）。
        # 否则 tools/list 暴露的工具 tools/call 会报「未启用」——MCP 语义要求
        # 暴露即可调用。
        if self.config.default_mount_compressor:
            for manifest in self.registry.default_mounted():
                if manifest.id not in self.lifecycle._instances:
                    try:
                        await self.lifecycle.enable(manifest.id)
                        logger.info("auto-mount: %s 已启用", manifest.id)
                    except Exception as exc:
                        logger.warning("auto-mount: %s 启用失败: %s", manifest.id, exc)

        # A4：从 config_center 恢复持久化的已安装插件（非默认挂载的手动安装项）
        for plugin_id in self.handler.restore_installed():
            if plugin_id in self.lifecycle._instances:
                continue
            if self.registry.get(plugin_id) is None:
                logger.warning("restore: 持久化的插件 %s 未注册，跳过", plugin_id)
                continue
            try:
                await self.lifecycle.enable(plugin_id)
                logger.info("restore: %s 已恢复启用", plugin_id)
            except Exception as exc:
                logger.warning("restore: %s 恢复失败: %s", plugin_id, exc)

        transport_type = transport or self.config.mcp_transport
        host = kwargs.get("host", self.config.mcp_host)
        port = kwargs.get("port", self.config.mcp_port)
        # R6：SSE/HTTP 远程传输鉴权 token（环境变量注入，不落盘配置）
        auth_token = kwargs.get(
            "auth_token", os.environ.get("FUSION_PLUGIN_AUTH_TOKEN")
        )

        # P0-1：远程传输非 loopback 绑定必须鉴权，防止对外裸奔全部 RPC
        if transport_type in ("sse", "http") and not _is_loopback_host(host):
            if not auth_token:
                msg = (
                    f"远程传输 {transport_type} 绑定非 loopback 地址 {host} 但未设置鉴权 token；"
                    "拒绝启动。请设置环境变量 FUSION_PLUGIN_AUTH_TOKEN 或仅绑 loopback。"
                )
                logger.error(msg)
                raise RuntimeError(msg)
            logger.warning(
                "远程传输 %s 绑定 %s 已启用 Bearer 鉴权", transport_type, host
            )

        self._transport = create_transport(
            transport_type,
            handler=self.handler.handle,
            host=host,
            port=port,
            auth_token=auth_token,
        )

        self._running = True
        logger.info(
            "MCPServer starting: transport=%s host=%s port=%s",
            transport_type,
            host,
            port,
        )

        # P1-2：启用 PROCESS 沙箱时启动看门狗，心跳判死生产生效
        if self.config.sandbox_default_mode == "process":
            self.lifecycle.start_watcher()
            logger.info("lifecycle watcher 已启动（process 沙箱心跳判死）")

        # P1-4：注册 atexit 兜底（SIGKILL 无法捕获，但优雅退出/异常时回收资源）
        if not self._atexit_registered:
            atexit.register(self._sync_cleanup)
            self._atexit_registered = True

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
        try:
            await self._stop_event.wait()
        finally:
            # E5：无论正常唤醒、异常还是 KeyboardInterrupt，均执行完整清理
            await self._full_shutdown()

    async def _full_shutdown(self) -> None:
        """完整停机：停看门狗 → 杀全部沙箱子进程 → flush token → 停传输。

        P1-2/P1-3/P1-4：补齐生产运维闭环，避免子进程孤儿与 token 丢失。
        """
        # P1-2：停看门狗（stop_watcher 为协程，须 await；同步调用会泄漏 watcher task）
        try:
            await self.lifecycle.stop_watcher()
        except Exception as exc:
            logger.warning("stop_watcher 失败: %s", exc)
        # P1-3：显式 kill 全部 PROCESS 沙箱子进程，防孤儿
        try:
            sandbox = getattr(self.lifecycle, "_sandbox", None)
            if sandbox is not None and hasattr(sandbox, "shutdown_all"):
                await sandbox.shutdown_all()
        except Exception as exc:
            logger.warning("sandbox shutdown_all 失败: %s", exc)
        # P1-4：flush 未落盘 token 记录
        try:
            self.token_meter.flush()
        except Exception as exc:
            logger.warning("token_meter flush 失败: %s", exc)
        # 停传输
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
        """停止 MCP Server：触发 stop_event 解除 start() 阻塞，并执行完整清理。

        _full_shutdown 幂等（_transport_stop 有 _running 守卫），
        故 stop() 与 start() 的 finally 调用不会双重回收。
        """
        if self._stop_event is not None:
            self._stop_event.set()
        await self._full_shutdown()

    def _sync_cleanup(self) -> None:
        """atexit 兜底：同步清理残留资源（事件循环已关闭时尽力而为）。

        SIGKILL 无法捕获；此处仅覆盖异常退出 / 未显式 await stop 的场景。
        """
        try:
            self.token_meter.flush()
        except Exception:
            pass

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
