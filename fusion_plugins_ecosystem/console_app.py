"""Extensions 控制台 HTTP 服务（issue #30 E3）。

FastAPI 应用：静态控制台（web/）+ REST API，端点契约与 f-ecommerce
控制台一致（仅去电商前缀），GUI 四页签（Connectors / Plugins·MCP /
Skills / Sub-agents，issue #41 能力中心 hub）。

独立于 fusion-plugin-server（MCP wire）运行：
    fusion-plugin-console --port 8795

issue #30 验收补充 — shell 模块化：
- /api/* 鉴权：FUSION_PLUGINS_CONSOLE_API_KEY 静态密钥 + 网关签发 fm- 模块密钥
  双活（gateway /shell/ 代理注入 Bearer）；两者皆未配置时放行（默认仅本机绑定）。
- FUSION_ADMIN_KEY 配置时启动后台注册循环（shell_registry，issue #38 单点解析），
  侧边栏出现扩展控制台入口，console 停止后入口置灰。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8795  # port-registry: fusion-plugins-ecosystem-console


def _accepted_keys() -> list[str]:
    # 双活窗口: env 静态密钥与网关签发 fm- 密钥同时有效 (对齐 gateway K1 grace)。
    keys = []
    static = os.environ.get("FUSION_PLUGINS_CONSOLE_API_KEY", "").strip()
    if static:
        keys.append(static)
    try:
        from fusion_plugins_ecosystem.shell_registry import load_module_key

        module = load_module_key()
        if module:
            keys.append(module)
    except Exception:
        pass
    return list(dict.fromkeys(keys))


def create_app(
    desk: Any = None,
    registry: Any = None,
    lifecycle: Any = None,
) -> Any:
    """构建 FastAPI 控制台应用（fastapi 为 console 可选依赖）。"""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
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
        version="0.9.0",
        lifespan=lifespan,
    )
    console = ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)
    console_holder["console"] = console
    app.state.console = console

    @app.middleware("http")
    async def _api_auth(request, call_next):
        # gateway /shell/ 代理注入 Bearer <fm- key | static key>; 未配置密钥时
        # 放行 (默认仅 127.0.0.1 绑定, 本地优先)。静态面不鉴权 — 会话在网关侧。
        if request.url.path.startswith("/api/"):
            keys = _accepted_keys()
            if keys:
                auth = (request.headers.get("authorization") or "").strip()
                token = (
                    auth[7:].strip()
                    if auth.lower().startswith("bearer ")
                    else (request.headers.get("x-api-key") or "").strip()
                )
                if token not in keys:
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "fusion-plugins-console"}

    @app.get("/api/whoami")
    async def whoami() -> dict[str, Any]:
        return {
            "user": "local",
            "role": "admin",
            "menus": [
                {"id": "extensions", "label": "Extensions", "href": "extensions.html"},
            ],
        }

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {"capabilities": console.list_capabilities()}

    # ── 能力中心 hub（issue #41）──

    @app.get("/api/hub")
    async def hub(q: str = "") -> dict[str, Any]:
        return console.list_hub(q)

    # ── 安装/授权/更新/卸载流（issue #42 PC-2）──

    @app.get("/api/hub/bundles")
    async def bundles_list() -> dict[str, Any]:
        return console.list_bundles()

    @app.post("/api/hub/bundles/{bundle_id}/install")
    async def bundles_install(bundle_id: str) -> dict[str, Any]:
        try:
            return await console.install_bundle(bundle_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/hub/bundles/{bundle_id}/items/{ref}/tick")
    async def bundle_item_tick(
        bundle_id: str, ref: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await console.tick_bundle_item(bundle_id, ref, bool(body.get("enabled")))

    @app.get("/api/hub/bundles/{bundle_id}/update")
    async def bundle_update_preview(bundle_id: str) -> dict[str, Any]:
        try:
            return console.bundle_update_preview(bundle_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/hub/bundles/{bundle_id}/update")
    async def bundle_update_apply(
        bundle_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return await console.bundle_update_apply(
                bundle_id, [str(r) for r in (body.get("opt_in") or [])]
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/hub/bundles/{bundle_id}/uninstall-preview")
    async def bundle_uninstall_preview(bundle_id: str) -> dict[str, Any]:
        return console.uninstall_preview(bundle_id)

    @app.delete("/api/hub/bundles/{bundle_id}")
    async def bundle_uninstall(bundle_id: str) -> dict[str, Any]:
        return await console.uninstall_bundle(bundle_id)

    @app.get("/api/hub/connectors/{connector_id}/oauth")
    async def connector_oauth_scopes(connector_id: str) -> dict[str, Any]:
        return console.oauth_scopes(connector_id)

    @app.post("/api/hub/connectors/{connector_id}/oauth")
    async def connector_oauth_complete(
        connector_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await console.oauth_complete(
            connector_id,
            [str(s) for s in (body.get("scopes") or [])],
            body.get("credentials") or {},
        )

    @app.get("/api/hub/connectors/{connector_id}/policy")
    async def connector_scope_policy(connector_id: str) -> dict[str, Any]:
        return console.scope_policy(connector_id)

    # ── 审计视图 + scope 撤销 + 反向查找（issue #43 PC-3）──

    @app.get("/api/hub/audit")
    async def hub_audit(
        resource_id: str = "",
        resource_type: str = "",
        action: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        return console.audit_records(
            resource_id=resource_id,
            resource_type=resource_type,
            action=action,
            trace_id=trace_id,
            limit=limit,
        )

    @app.post("/api/hub/connectors/{connector_id}/revoke")
    async def connector_revoke_scopes(
        connector_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await console.revoke_scopes(
            connector_id, [str(s) for s in (body.get("scopes") or [])]
        )

    @app.get("/api/hub/capabilities/{capability}/used-by")
    async def capability_used_by(capability: str) -> dict[str, Any]:
        return console.capability_used_by(capability)

    # ── 稳定注册表读契约（issue #44 PC-4：cowork#228 / agent-studio#529 消费）──

    @app.get("/api/registry/capabilities")
    async def registry_capabilities() -> dict[str, Any]:
        return console.registry_capabilities()

    @app.get("/api/registry/capabilities/{capability}")
    async def registry_capability(capability: str) -> dict[str, Any]:
        out = console.registry_capability(capability)
        if "error" in out:
            raise HTTPException(status_code=404, detail=out["error"])
        return out

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

    @app.delete("/api/extensions/connectors/{connector_id}")
    async def connectors_remove(connector_id: str) -> dict[str, Any]:
        return await console.remove_connector(connector_id)

    @app.post("/api/extensions/connectors/{connector_id}/harness")
    async def connectors_harness(connector_id: str) -> dict[str, Any]:
        return await console.harness(connector_id)

    # ── 凭据保险库（Desk Fernet 存储）──

    @app.put("/api/adapters/{credential_ref}")
    async def adapters_put(credential_ref: str, body: dict[str, Any]) -> dict[str, Any]:
        creds = body.get("credentials")
        if not isinstance(creds, dict):
            raise HTTPException(
                status_code=422, detail="body.credentials must be an object"
            )
        out = console.save_credentials(credential_ref, creds)
        if not out.get("saved"):
            raise HTTPException(
                status_code=400, detail=out.get("error") or "credential save failed"
            )
        return out

    # ── plugins ──

    @app.get("/api/extensions/plugins")
    async def plugins_list() -> dict[str, Any]:
        return console.list_plugins()

    @app.post("/api/extensions/plugins")
    async def plugins_install(body: dict[str, Any]) -> dict[str, Any]:
        return console.install_plugin(str(body.get("source") or ""))

    @app.post("/api/extensions/plugins/{plugin_name}/tick")
    async def plugins_tick(plugin_name: str, body: dict[str, Any]) -> dict[str, Any]:
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
    async def mcp_tick(server_id: str, body: dict[str, Any]) -> dict[str, Any]:
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
            return FileResponse(
                WEB_DIR / "shell.js", media_type="application/javascript"
            )

        @app.get("/extensions.html")
        async def extensions_html() -> FileResponse:
            return FileResponse(WEB_DIR / "extensions.html")

    return app


def main() -> None:
    """fusion-plugin-console CLI 入口。"""
    import argparse

    from fusion_core.config import service_port

    parser = argparse.ArgumentParser(
        prog="fusion-plugin-console",
        description="Fusion Plugins — Extensions Console (Connectors + Plugins·MCP)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    # issue #38：端口单点解析 — env FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT 可覆盖
    parser.add_argument(
        "--port",
        type=int,
        default=service_port("plugins-ecosystem-console", default=DEFAULT_PORT),
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    # issue #35：进程标题带产品名 + 端口，ps 可区分实例
    from fusion_plugins_ecosystem.proctitle import console_title, set_process_title

    set_process_title(console_title(args.port))

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

    import asyncio

    from fusion_plugins_ecosystem.shell_registry import (
        shell_registration_enabled,
        shell_registry_loop,
    )

    app = create_app()
    register_enabled = shell_registration_enabled()
    logger.info(
        "console: 监听 http://%s:%s (shell 注册 %s)",
        args.host,
        args.port,
        "启用" if register_enabled else "未启用 — 缺 FUSION_ADMIN_KEY",
    )

    async def _serve():
        registry_task = (
            asyncio.create_task(shell_registry_loop()) if register_enabled else None
        )
        config = uvicorn.Config(
            app, host=args.host, port=args.port, log_level=args.log_level.lower()
        )
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if registry_task:
                registry_task.cancel()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
