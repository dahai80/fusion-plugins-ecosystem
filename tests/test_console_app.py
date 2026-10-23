"""Extensions 控制台 HTTP 服务测试（FastAPI TestClient，端点契约对齐 GUI）。"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from fusion_plugins_ecosystem.console_app import WEB_DIR, create_app  # noqa: E402
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle  # noqa: E402
from fusion_plugins_ecosystem.registry import PluginRegistry  # noqa: E402

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from test_extensions_console import FakeDesk  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))
    # 密钥隔离: 真实部署可能存在 ~/.fusion-plugins-ecosystem/web_module_key
    # (fm- 签发密钥), 不能让本机部署凭据影响测试的 /api/* 鉴权行为。
    monkeypatch.delenv("FUSION_PLUGINS_CONSOLE_API_KEY", raising=False)
    from fusion_plugins_ecosystem import shell_registry

    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key")


class _McpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        method = req.get("method")
        if method == "initialize":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {"serverInfo": {"name": "mock"}},
                }
            )
        elif method == "tools/list":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {
                        "tools": [
                            {"name": "t1", "description": "tool one"},
                            {"name": "t2", "description": "tool two"},
                        ]
                    },
                }
            )
        else:
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32601, "message": "no method"},
                }
            )


@pytest.fixture()
def mcp_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


@pytest.fixture()
def client():
    desk = FakeDesk()
    registry = PluginRegistry(desk)
    lifecycle = PluginLifecycle(registry)
    return TestClient(create_app(desk=desk, registry=registry, lifecycle=lifecycle))


CONN_DOC = {
    "id": "erp-json",
    "kind": "rest",
    "base_url": "https://erp.example.com/api",
    "credential_ref": "erp-json",
    "auth": {"type": "bearer", "credential_fields": ["api_key"]},
    "operations": {
        "erp.stock.fetch": {
            "method": "GET",
            "path": "/stock",
            "response": {"rows": "$.rows"},
        },
        "erp.order.push": {"method": "POST", "path": "/orders"},
    },
}


def test_whoami_and_capabilities(client):
    me = client.get("/api/whoami").json()
    assert me["role"] == "admin"
    assert me["menus"][0]["href"] == "extensions.html"
    assert client.get("/api/capabilities").json() == {"capabilities": []}


def test_connector_full_flow(client):
    out = client.post("/api/extensions/connectors", json=CONN_DOC).json()
    assert out["installed"] is True
    bad = client.post("/api/extensions/connectors", json={"id": "x"}).json()
    assert bad["installed"] is False
    listing = client.get("/api/extensions/connectors").json()
    assert listing["connectors"][0]["id"] == "erp-json"

    creds = client.put("/api/adapters/erp-json", json={"credentials": {"api_key": "k"}})
    assert creds.json() == {"saved": True, "credential_ref": "erp-json"}
    bad_creds = client.put("/api/adapters/erp-json", json={"nope": 1})
    assert bad_creds.status_code == 422

    enable = client.post("/api/extensions/connectors/erp-json/enable").json()
    assert enable["capabilities"] == ["connector.erp-json.erp.stock.fetch"]
    caps = client.get("/api/capabilities").json()["capabilities"]
    assert caps[0]["source"] == "connector"

    disable = client.post("/api/extensions/connectors/erp-json/disable").json()
    assert disable["capabilities_disabled"] == ["connector__erp-json__erp.stock.fetch"]

    harness = client.post("/api/extensions/connectors/erp-json/harness").json()
    assert harness["conformant"] is False


def test_mcp_flow(client, mcp_server):
    out = client.post(
        "/api/extensions/mcp", json={"id": "erp-mcp", "url": mcp_server}
    ).json()
    assert out["registered"] is True
    assert out["candidate_tools"] == ["t1", "t2"]
    probe_fail = client.post(
        "/api/extensions/mcp", json={"id": "dead", "url": "http://127.0.0.1:1/mcp"}
    ).json()
    assert probe_fail["registered"] is False

    tick = client.post(
        "/api/extensions/mcp/erp-mcp/tick", json={"tools": ["t1"]}
    ).json()
    assert tick["ticked"] == ["mcp.erp-mcp.t1"]
    servers = client.get("/api/extensions/mcp").json()["servers"]
    assert servers[0]["ticked"] == ["t1"]

    rm = client.delete("/api/extensions/mcp/erp-mcp").json()
    assert rm["capabilities_removed"] == ["mcp__erp-mcp__t1"]


def test_plugins_flow(client):
    plugins = client.get("/api/extensions/plugins").json()
    assert plugins == {"plugins": [], "require_signing": False}
    out = client.post("/api/extensions/plugins", json={"source": "/nonexistent"}).json()
    assert out["installed"] is False


def test_static_console_served(client):
    assert WEB_DIR.exists()
    r = client.get("/")
    assert r.status_code == 200
    assert "Extensions" in r.text
    assert 'data-tab="connectors"' in r.text
    assert 'data-tab="plugins"' in r.text
    assert client.get("/app.css").status_code == 200
    assert client.get("/shell.js").status_code == 200
    assert client.get("/extensions.html").status_code == 200


def test_theme_follows_shell_host(client):
    """issue #33 — 嵌入跟随宿主主题推送, standalone 读 shell localStorage。"""
    js = client.get("/shell.js").text
    # 嵌入: 监听宿主 {type:"event", event:"theme"} postMessage (同源校验)
    assert '"event"' in js and '"theme"' in js
    assert "e.origin !== location.origin" in js
    assert 'msg.payload === "light" || msg.payload === "dark"' in js
    # 类主题: html.light / html.dark 双类切换
    assert 'classList.toggle("light"' in js
    assert 'classList.toggle("dark"' in js
    # standalone: 初始主题读 shell 同键 localStorage, 兼容旧 cookie
    assert 'localStorage.getItem("fusion.shell.theme")' in js
    assert "fs_theme" in js
    # prefers-color-scheme 初始默认
    assert "prefers-color-scheme: light" in js


class TestApiAuth:
    """issue #30 验收补充 — /api/* 鉴权 (静态 env key + 签发 fm- key 双活)。"""

    def test_no_keys_configured_api_open(self, client):
        assert client.get("/api/whoami").status_code == 200

    def test_static_key_enforced(self, client, monkeypatch):
        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_API_KEY", "static-secret")
        assert client.get("/api/whoami").status_code == 401
        assert (
            client.get(
                "/api/whoami", headers={"Authorization": "Bearer static-secret"}
            ).status_code
            == 200
        )

    def test_x_api_key_header_accepted(self, client, monkeypatch):
        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_API_KEY", "static-secret")
        assert (
            client.get(
                "/api/whoami", headers={"x-api-key": "static-secret"}
            ).status_code
            == 200
        )

    def test_module_key_accepted(self, client, monkeypatch, tmp_path):
        from fusion_plugins_ecosystem import shell_registry

        monkeypatch.setattr(
            shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key"
        )
        shell_registry.save_module_key("fm-issued-key-0001")
        assert client.get("/api/whoami").status_code == 401
        assert (
            client.get(
                "/api/whoami", headers={"Authorization": "Bearer fm-issued-key-0001"}
            ).status_code
            == 200
        )

    def test_static_and_module_key_dual_active(self, client, monkeypatch, tmp_path):
        from fusion_plugins_ecosystem import shell_registry

        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_API_KEY", "static-secret")
        monkeypatch.setattr(
            shell_registry, "MODULE_KEY_PATH", tmp_path / "web_module_key"
        )
        shell_registry.save_module_key("fm-issued-key-0002")
        assert (
            client.get(
                "/api/whoami", headers={"Authorization": "Bearer static-secret"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/whoami", headers={"Authorization": "Bearer fm-issued-key-0002"}
            ).status_code
            == 200
        )

    def test_static_page_not_gated(self, client, monkeypatch):
        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_API_KEY", "static-secret")
        assert client.get("/").status_code == 200
        assert client.get("/extensions.html").status_code == 200


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "service": "fusion-plugins-console"}
