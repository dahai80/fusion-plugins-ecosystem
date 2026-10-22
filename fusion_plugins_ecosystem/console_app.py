"""Extensions 控制台 HTTP 服务（issue #30 E3）。

FastAPI 应用：静态控制台（web/）+ REST API，端点契约与 f-ecommerce
控制台一致（仅去电商前缀），GUI 两页签（Connectors / Plugins·MCP）。

独立于 fusion-plugin-server（MCP wire）运行：
    fusion-plugin-console --port 8795
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8795


def create_app(
    desk: Any = None,
    registry: Any = None,
    lifecycle: Any = None,
) -> Any:
    """构建 FastAPI 控制台应用（fastapi 为 console 可选依赖）。"""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover — 环境缺依赖
        raise RuntimeError(
            "console 需要 fastapi：pip install 'fusion-plugins-ecosystem[console]'"
        ) from exc

    from contextlib import asynccontextmanager

    from fusion_plugins_ecosystem.extensions_console import ExtensionsConsole

    console_holder: dict[str, ExtensionsConsole] = {}

    @asynccontextmanager
    async def lifespan(_app):
        console = console_holder["console"]
        out = await console.resync()
        logger.info("console: startup resync 重建 %d 个能力", len(out["relisted"]))
        yield

    app = FastAPI(
        title="Fusion Plugins — Extensions Console",
        version="0.4.3",
        lifespan=lifespan,
    )
    console = ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)
    console_holder["console"] = console
    app.state.console = console

    @app.get("/api/whoami")
    async def whoami() -> dict[str, Any]:
        return {
            "user": "local",
            "role": "admin",
            "menus": [
                {"id": "extensions", "label": "Extensions",
                 "href": "extensions.html"},
            ],
        }

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {"capabilities": console.list_capabilities()}

    # ── connectors ──

    @app.get("/api/extensions/connectors")
    async def connectors_list() -> dict[str, Any]:
        return console.list_connectors()

    @app.post("/api/extensions/connectors")
    async def connectors_install(body: dict[str, Any]) -> dict[str, Any]:
        return console.install_connector(body)

    @app.post("/api/extensions/connectors/{connector_id}/enable")
    async def connectors_enable(connector_id: str) -> dict[str, Any]:
        return await console.enable_connector(connector_id)

    @app.post("/api/extensions/connectors/{connector_id}/disable")
    async def connectors_disable(connector_id: str) -> dict[str, Any]:
        return await console.disable_connector(connector_id)

    @app.post("/api/extensions/connectors/{connector_id}/harness")
    async def connectors_harness(connector_id: str) -> dict[str, Any]:
        return await console.harness(connector_id)

    # ── 凭据保险库（Desk Fernet 存储）──

    @app.put("/api/adapters/{credential_ref}")
    async def adapters_put(
        credential_ref: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        creds = body.get("credentials")
        if not isinstance(creds, dict):
            raise HTTPException(status_code=422,
                                detail="body.credentials must be an object")
        out = console.save_credentials(credential_ref, creds)
        if not out.get("saved"):
            raise HTTPException(status_code=400,
                                detail=out.get("error") or "credential save failed")
        return out

    # ── plugins ──

    @app.get("/api/extensions/plugins")
    async def plugins_list() -> dict[str, Any]:
        return console.list_plugins()

    @app.post("/api/extensions/plugins")
    async def plugins_install(body: dict[str, Any]) -> dict[str, Any]:
        return console.install_plugin(str(body.get("source") or ""))

    @app.post("/api/extensions/plugins/{plugin_name}/tick")
    async def plugins_tick(
        plugin_name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await console.tick_nodes(
            plugin_name, [str(n) for n in (body.get("nodes") or [])]
        )

    @app.delete("/api/extensions/plugins/{plugin_name}")
    async def plugins_remove(plugin_name: str) -> dict[str, Any]:
        return await console.remove_plugin(plugin_name)

    # ── remote MCP intake ──

    @app.get("/api/extensions/mcp")
    async def mcp_list() -> dict[str, Any]:
        return {"servers": console.list_mcp_servers()}

    @app.post("/api/extensions/mcp")
    async def mcp_register(body: dict[str, Any]) -> dict[str, Any]:
        return console.register_mcp_server(
            str(body.get("id") or ""),
            str(body.get("url") or ""),
            str(body.get("credential_ref") or ""),
        )

    @app.post("/api/extensions/mcp/{server_id}/tick")
    async def mcp_tick(
        server_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await console.tick_mcp_tools(
            server_id, [str(t) for t in (body.get("tools") or [])]
        )

    @app.delete("/api/extensions/mcp/{server_id}")
    async def mcp_remove(server_id: str) -> dict[str, Any]:
        return console.remove_mcp_server(server_id)

    # ── 静态控制台 ──

    if WEB_DIR.exists():

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "extensions.html")

        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/app.css")
        async def app_css() -> FileResponse:
            return FileResponse(WEB_DIR / "app.css", media_type="text/css")

        @app.get("/shell.js")
        async def shell_js() -> FileResponse:
            return FileResponse(WEB_DIR / "shell.js",
                                media_type="application/javascript")

        @app.get("/extensions.html")
        async def extensions_html() -> FileResponse:
            return FileResponse(WEB_DIR / "extensions.html")

    return app


def main() -> None:
    """fusion-plugin-console CLI 入口。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="fusion-plugin-console",
        description="Fusion Plugins — Extensions Console (Connectors + Plugins·MCP)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "console 需要 uvicorn：pip install 'fusion-plugins-ecosystem[console]'"
        ) from exc

    app = create_app()
    logger.info("console: 监听 http://%s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
