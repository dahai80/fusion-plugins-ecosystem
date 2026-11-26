# MCP Server entry point.
#
# Composes transport + JSON-RPC handler + runtime and provides the
# fusion-plugin-server command entry.

from __future__ import annotations

import argparse
import asyncio
import atexit
import ipaddress
import logging
import os
import signal
from typing import Any

from fusion_plugins_ecosystem import __version__
from fusion_plugins_ecosystem.config import EcosystemConfig
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.jsonrpc import MCPHandler
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle
from fusion_plugins_ecosystem.registry import PluginRegistry
from fusion_plugins_ecosystem.token_meter import TokenMeter
from fusion_plugins_ecosystem.transport import create_transport

logger = logging.getLogger(__name__)


def _is_loopback_host(host: str | None) -> bool:
    # Whether host is loopback (127.0.0.0/8, ::1, localhost).
    #
    # P0-1: remote transports (sse/http) force auth when bound to a
    # non-loopback address; loopback is allowed by default (local
    # single-machine / test scenarios).
    if not host:
        return True
    if host in ("localhost", "::1"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        # Non-IP literals (e.g. hostnames) are conservatively treated as
        # non-loopback
        return False


class MCPServer:
    # MCP Server entry point, composing transport + handler + runtime.
    #
    # Usage:
    #     server = MCPServer()
    #     await server.start(transport="stdio")
    #
    # Or via CLI:
    #     fusion-plugin-server --transport stdio
    #     fusion-plugin-server --transport sse --port 8765

    def __init__(self, config: EcosystemConfig | None = None) -> None:
        self.config = config or EcosystemConfig()
        self.desk = DeskRuntime(
            mcp_gateway_port=self.config.mcp_port or None,
        )
        # A3: sync vram_limit_mb into the DeskRuntime total vRAM budget
        if self.config.vram_limit_mb:
            self.desk.vram_total_mb = self.config.vram_limit_mb
        self.registry = PluginRegistry(desk=self.desk)
        # A2: inject config into lifecycle; timeout/restart/heartbeat
        # thresholds are now config-driven
        self.lifecycle = PluginLifecycle(self.registry, config=self.config)
        # A5: inject configured metering thresholds and persist path into
        # token_meter
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
        # A4: restore persisted config and the installed-plugin set from the
        # config center at startup
        self.handler.restore_config()
        # A3: apply the configured log level to the root logger (the CLI
        # --log-level flag wins; this covers the programmatic entry)
        if self.config.log_level and self.config.log_level != "INFO":
            logging.getLogger().setLevel(getattr(logging, self.config.log_level, logging.INFO))
        self._transport: Any = None
        self._running = False
        # Stop signal: start() waits on it, stop() sets it; keeps the
        # process alive/shuts it down across coroutines
        self._stop_event: asyncio.Event | None = None
        # P1-4: idempotence guard for atexit registration
        self._atexit_registered = False

    async def start(
        self,
        transport: str = "stdio",
        **kwargs: Any,
    ) -> None:
        # Start the MCP Server.
        if self._running:
            logger.warning("MCPServer already running")
            return

        self.registry.register_builtin()

        # Default mounting: when default_mount_compressor is on, all
        # default_mounted plugins are auto load+enabled so tools/call can
        # invoke them directly (no extra install). Otherwise tools/call on
        # tools/list-exposed tools would fail with "not enabled" — MCP
        # semantics require exposed = callable.
        if self.config.default_mount_compressor:
            for manifest in self.registry.default_mounted():
                if manifest.id not in self.lifecycle._instances:
                    try:
                        await self.lifecycle.enable(manifest.id)
                        logger.info("auto-mount: %s enabled", manifest.id)
                    except Exception as exc:
                        logger.warning("auto-mount: failed to enable %s: %s", manifest.id, exc)

        # A4: restore persisted installed plugins from the config center
        # (manual installs, not the default-mounted set)
        for plugin_id in self.handler.restore_installed():
            if plugin_id in self.lifecycle._instances:
                continue
            if self.registry.get(plugin_id) is None:
                logger.warning("restore: persisted plugin %s not registered, skipping", plugin_id)
                continue
            try:
                await self.lifecycle.enable(plugin_id)
                logger.info("restore: %s re-enabled", plugin_id)
            except Exception as exc:
                logger.warning("restore: failed to restore %s: %s", plugin_id, exc)

        transport_type = transport or self.config.mcp_transport
        host = kwargs.get("host", self.config.mcp_host)
        port = kwargs.get("port", self.config.mcp_port)
        # R6: auth token for SSE/HTTP remote transports (env-injected, never
        # written to config files)
        auth_token = kwargs.get("auth_token", os.environ.get("FUSION_PLUGIN_AUTH_TOKEN"))

        # P0-1: non-loopback remote transport bindings must have auth, to
        # avoid exposing every RPC to the network
        if transport_type in ("sse", "http") and not _is_loopback_host(host):
            if not auth_token:
                msg = (
                    f"remote transport {transport_type} bound to non-loopback address {host} "
                    "without an auth token; refusing to start. Set the FUSION_PLUGIN_AUTH_TOKEN "
                    "env var or bind to loopback only."
                )
                logger.error(msg)
                raise RuntimeError(msg)
            logger.warning("remote transport %s bound to %s with Bearer auth enabled", transport_type, host)

        self._transport = create_transport(
            transport_type,
            handler=self.handler.handle,
            host=host,
            port=port,
            auth_token=auth_token,
            metrics_provider=lambda: self.desk.metrics.render(),
        )

        self._running = True
        logger.info(
            "MCPServer starting: transport=%s host=%s port=%s",
            transport_type,
            host,
            port,
        )

        # P1-2: start the watchdog when the PROCESS sandbox is enabled so
        # heartbeat-based death detection works in production
        if self.config.sandbox_default_mode == "process":
            self.lifecycle.start_watcher()
            logger.info("lifecycle watcher started (process sandbox heartbeat death detection)")

        # P1-4: register the atexit fallback (SIGKILL cannot be caught, but
        # resources are reclaimed on graceful exit/exceptions)
        if not self._atexit_registered:
            atexit.register(self._sync_cleanup)
            self._atexit_registered = True

        await self._transport.start()

        # Every transport type must block until the stop signal; otherwise
        # start() returns immediately, asyncio.run finishes and the process
        # exits (sse/http previously had no keep-alive, so the CLI flashed
        # and exited)
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Some platforms lack add_signal_handler; fall back to polling
                pass
        try:
            await self._stop_event.wait()
        finally:
            # E5: run the full cleanup on normal wakeup, exceptions, and
            # KeyboardInterrupt alike
            await self._full_shutdown()

    async def _full_shutdown(self) -> None:
        # Full shutdown: stop watchdog → kill all sandbox subprocesses →
        # flush tokens → stop transport.
        #
        # P1-2/P1-3/P1-4: completes the production ops loop, preventing
        # orphan subprocesses and token record loss.
        # P1-2: stop the watchdog (stop_watcher is a coroutine and must be
        # awaited; a sync call would leak the watcher task)
        try:
            await self.lifecycle.stop_watcher()
        except Exception as exc:
            logger.warning("stop_watcher failed: %s", exc)
        # P1-3: explicitly kill all PROCESS sandbox subprocesses to prevent
        # orphans
        try:
            sandbox = getattr(self.lifecycle, "_sandbox", None)
            if sandbox is not None and hasattr(sandbox, "shutdown_all"):
                await sandbox.shutdown_all()
        except Exception as exc:
            logger.warning("sandbox shutdown_all failed: %s", exc)
        # P1-4: flush unpersisted token records
        try:
            self.token_meter.flush()
        except Exception as exc:
            logger.warning("token_meter flush failed: %s", exc)
        # Stop the transport
        await self._transport_stop()

    async def _transport_stop(self) -> None:
        # Idempotent transport stop: the _running guard prevents double
        # stop from start()/stop().
        if not self._running:
            return
        self._running = False
        if self._transport:
            await self._transport.stop()
        logger.info("MCPServer stopped")

    async def stop(self) -> None:
        # Stop the MCP Server: sets stop_event to unblock start() and runs
        # the full cleanup.
        #
        # _full_shutdown is idempotent (_transport_stop has the _running
        # guard), so stop() and start()'s finally clause never double-reclaim.
        if self._stop_event is not None:
            self._stop_event.set()
        await self._full_shutdown()

    def _sync_cleanup(self) -> None:
        # atexit fallback: synchronously clean up leftover resources
        # (best-effort when the event loop is already closed).
        #
        # SIGKILL cannot be caught; this only covers abnormal exits /
        # paths that never awaited stop().
        try:
            self.token_meter.flush()
        except Exception:
            pass

    @property
    def transport(self) -> Any:
        return self._transport


def main() -> None:
    # CLI entry: the fusion-plugin-server command.
    from fusion_core.config import service_port

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
        # issue #38: single-point port resolution — env
        # FUSION_PLUGINS_ECOSYSTEM_PORT can pin it (port-registry: 8765);
        # default 0 keeps auto.
        default=service_port("plugins-ecosystem", default=0),
        help="Port for SSE/HTTP transport (0=auto)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args()

    # issue #35: process title carries the product name + transport/port so
    # ps can tell instances apart
    from fusion_plugins_ecosystem.proctitle import (
        plugin_server_title,
        set_process_title,
    )

    set_process_title(plugin_server_title(args.transport, args.port))

    # P3-6: structured JSON logging + correlation_id (see logging_setup).
    # Replaces the old plain-text basicConfig for ELK/Loki aggregation and
    # request correlation.
    from fusion_plugins_ecosystem.logging_setup import configure_structured_logging

    configure_structured_logging(level=getattr(logging, args.log_level))

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
