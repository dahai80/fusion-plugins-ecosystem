"""ExtensionsConsole 测试：连接器 install/enable/harness、MCP intake、插件 tick、resync。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fusion_plugins_ecosystem.extensions_console as ec
from fusion_plugins_ecosystem.extensions_console import (
    ExtensionsConsole,
    _vault_credentials,
    _vault_headers,
)
from fusion_plugins_ecosystem.lifecycle import PluginLifecycle, PluginState
from fusion_plugins_ecosystem.registry import PluginRegistry


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))


class FakeMetrics:
    def gauge(self, name, help_text=""):
        return self

    def counter(self, name, help_text=""):
        return self

    def set(self, *a, **kw):
        pass

    def inc(self, *a, **kw):
        pass


class FakeDesk:
    def __init__(self):
        self._keys = {}
        self.logs = []
        self.metrics = FakeMetrics()
        self.registered_plugin_ids = set()

    def log(self, plugin_id, level, message, **kw):
        self.logs.append((plugin_id, level, message))

    def get_api_key(self, provider):
        return self._keys.get(provider)

    def set_api_key(self, provider, key):
        self._keys[provider] = key


class _RestHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/stock"):
            if self.headers.get("Authorization") != "Bearer tok-1":
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"rows": [{"sku": "A1"}], "total": 1})
        else:
            self._json(404, {"error": "no route"})


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RestHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ── mock MCP server（streamable-http JSON-RPC）──


class _McpHandler(BaseHTTPRequestHandler):
    server_state = {
        "tools": [
            {
                "name": "erp.query",
                "description": "query erp",
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
            {"name": "erp.push", "description": "push data"},
        ]
    }

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
        if self.headers.get("Authorization") == "Bearer good":
            method = req.get("method")
            if method == "initialize":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {"serverInfo": {"name": "mock-mcp"}},
                    }
                )
            elif method == "tools/list":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": {"tools": self.server_state["tools"]},
                    }
                )
            elif method == "tools/call":
                if req["params"]["name"] == "erp.query":
                    self._json(
                        {
                            "jsonrpc": "2.0",
                            "id": req["id"],
                            "result": {
                                "content": [{"type": "text", "text": "42"}],
                                "isError": False,
                            },
                        }
                    )
                else:
                    self._json(
                        {
                            "jsonrpc": "2.0",
                            "id": req["id"],
                            "result": {"content": [], "isError": True},
                        }
                    )
            else:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "error": {"code": -32601, "message": "no method"},
                    }
                )
        else:
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32001, "message": "unauthorized"},
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
def console():
    desk = FakeDesk()
    registry = PluginRegistry(desk)
    lifecycle = PluginLifecycle(registry)
    return ExtensionsConsole(desk=desk, registry=registry, lifecycle=lifecycle)


def _conn_doc(base_url="https://erp.example.com/api"):
    return {
        "id": "erp-json",
        "kind": "rest",
        "base_url": base_url,
        "credential_ref": "erp-json",
        "auth": {"type": "bearer", "credential_fields": ["token"]},
        "operations": {
            "erp.stock.fetch": {
                "method": "GET",
                "path": "/stock",
                "response": {"rows": "$.rows"},
            },
            "erp.order.push": {"method": "POST", "path": "/orders"},
        },
    }


# ── 连接器 ──


def test_install_connector_invalid(console):
    out = console.install_connector({"id": "x"})
    assert out["installed"] is False
    assert out["problems"]


def test_install_connector_ok(console):
    out = console.install_connector(_conn_doc())
    assert out["installed"] is True
    assert out["read_channels"] == ["erp.stock.fetch"]
    assert out["write_channels_review"] == ["erp.order.push"]
    listing = console.list_connectors()
    assert listing["connectors"][0]["problems"] == []
    assert listing["connectors"][0]["spine_capabilities"] == []


async def test_enable_disable_connector(console):
    console.install_connector(_conn_doc())
    out = await console.enable_connector("erp-json")
    assert out["enabled"] is True
    assert out["capabilities"] == ["connector.erp-json.erp.stock.fetch"]
    assert console.registry.get("connector__erp-json__erp.stock.fetch")
    caps = console.list_capabilities()
    assert caps[0]["id"] == "connector.erp-json.erp.stock.fetch"
    assert caps[0]["status"] == "active"
    assert caps[0]["source"] == "connector"

    out = await console.disable_connector("erp-json")
    assert out["capabilities_disabled"] == ["connector__erp-json__erp.stock.fetch"]
    assert console.registry.get("connector__erp-json__erp.stock.fetch") is None
    assert console.list_capabilities()[0]["status"] == "disabled"
    # 文档保留在磁盘（_disabled 标记）
    assert ec.get_connector("erp-json")["_disabled"] is True


async def test_enable_missing_connector(console):
    assert await console.enable_connector("ghost") == {
        "enabled": False,
        "error": "connector not found",
    }


def test_save_credentials(console):
    out = console.save_credentials("erp-json", {"token": "tok-1"})
    assert out["saved"] is True
    assert _vault_credentials(console.desk, "erp-json") == {"token": "tok-1"}
    bad = console.save_credentials("", {})
    assert bad["saved"] is False


class _DeadVaultDesk(FakeDesk):
    """模拟 standalone console：set/get 都是 no-op（无 config_center）。"""

    def set_api_key(self, provider, key):
        pass

    def get_api_key(self, provider):
        return None


def test_standalone_console_builds_own_lifecycle():
    """create_app 不传 lifecycle：console 自建，否则 tick/enable 全静默 no-op。"""
    desk = FakeDesk()
    console = ExtensionsConsole(desk=desk)
    assert console.lifecycle is not None
    assert console.lifecycle.registry is console.registry


def test_save_credentials_local_vault_fallback(tmp_path):
    desk = _DeadVaultDesk()
    registry = PluginRegistry(desk)
    console = ExtensionsConsole(desk=desk, registry=registry)
    out = console.save_credentials("erp-json", {"api_key": "k-9"})
    assert out["saved"] is True
    assert _vault_credentials(desk, "erp-json") == {"api_key": "k-9"}
    vault_file = tmp_path / "extensions" / "vault.json"
    assert vault_file.exists()
    assert "k-9" not in vault_file.read_text(encoding="utf-8")  # 密文落盘


# ── harness ──


async def test_harness_static_problems(console):
    console.install_connector({"id": "x"})
    out = await console.harness("x")
    assert out["conformant"] is False
    assert out["problems"]


async def test_harness_missing_connector(console):
    out = await console.harness("ghost")
    assert out["problems"] == ["connector not found"]


async def test_harness_live_check_no_creds(console, server):
    console.install_connector(_conn_doc(server))
    out = await console.harness("erp-json")
    assert out["conformant"] is False
    assert any(
        "missing credential" in p or "live check failed" in p for p in out["problems"]
    )


async def test_harness_live_check_ok(console, server):
    console.install_connector(_conn_doc(server))
    console.save_credentials("erp-json", {"token": "tok-1"})
    out = await console.harness("erp-json")
    assert out == {"connector_id": "erp-json", "problems": [], "conformant": True}


# ── 保险库头解析 ──


def test_vault_headers_shapes():
    desk = FakeDesk()
    assert _vault_headers(desk, "") == {}
    desk.set_api_key("a", json.dumps({"api_key": "k1"}))
    assert _vault_headers(desk, "a") == {"Authorization": "Bearer k1"}
    desk.set_api_key("b", json.dumps({"headers": {"X-A": "v"}}))
    assert _vault_headers(desk, "b") == {"X-A": "v"}
    desk.set_api_key("c", json.dumps({"weird": 1}))
    with pytest.raises(RuntimeError):
        _vault_headers(desk, "c")
    with pytest.raises(RuntimeError):
        _vault_headers(desk, "missing")


# ── REST 读操作执行（经桥接插件）──


async def test_bridged_rest_op_execution(console, server):
    console.install_connector(_conn_doc(server))
    console.save_credentials("erp-json", {"token": "tok-1"})
    await console.enable_connector("erp-json")
    manifest = console.registry.get("connector__erp-json__erp.stock.fetch")
    result = await manifest.entry_point(console.desk, {})
    assert result["ok"] is True
    assert result["extracted"]["rows"]


# ── MCP intake ──


def test_register_mcp_bad_inputs(console):
    assert console.register_mcp_server("", "http://x")["registered"] is False
    assert console.register_mcp_server("a/b", "http://x")["registered"] is False
    out = console.register_mcp_server("ok", "stdio://x")
    assert "url must be http(s)" in out["error"]
    out = console.register_mcp_server("ok", "http://127.0.0.1:1/mcp", "no-such-cred")
    assert "no vault credentials" in out["error"]


def test_register_mcp_probe_fail(console):
    out = console.register_mcp_server("bad", "http://127.0.0.1:1/mcp")
    assert out["registered"] is False
    assert out["error"].startswith("probe failed:")


def test_register_mcp_probe_auth_fail(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "wrong"})
    out = console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    assert out["registered"] is False
    assert "probe failed" in out["error"]


async def test_register_and_tick_mcp(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    out = console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    assert out["registered"] is True
    assert out["candidate_tools"] == ["erp.query", "erp.push"]

    tick = await console.tick_mcp_tools("erp-mcp", ["erp.query", "ghost"])
    assert tick["ticked"] == ["mcp.erp-mcp.erp.query"]
    assert tick["errors"] == ["'ghost' is not a tool of 'erp-mcp'"]
    assert console.registry.get("mcp__erp-mcp__erp.query")
    assert console.list_mcp_servers()[0]["ticked"] == ["erp.query"]

    caps = [c for c in console.list_capabilities() if c["source"] == "mcp"]
    assert caps[0]["id"] == "mcp.erp-mcp.erp.query"


async def test_call_mcp_tool(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query", "erp.push"])
    out = await console.call_mcp_tool("erp-mcp", "erp.query", {"q": "stock"})
    assert out["ok"] is True
    assert out["result"]["content"][0]["text"] == "42"
    manifest = console.registry.get("mcp__erp-mcp__erp.query")
    result = await manifest.entry_point(console.desk, {"q": "stock"})
    assert result["content"][0]["text"] == "42"
    bad = await console.call_mcp_tool("erp-mcp", "erp.push", {})
    assert bad["ok"] is True and bad["result"]["isError"] is True
    unknown = await console.call_mcp_tool("nope", "x", {})
    assert unknown["ok"] is False


async def test_remove_mcp_server(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query"])
    out = console.remove_mcp_server("erp-mcp")
    assert out["capabilities_removed"] == ["mcp__erp-mcp__erp.query"]
    assert console.list_mcp_servers() == []
    assert console.registry.get("mcp__erp-mcp__erp.query") is None


# ── 插件 tick / remove / install ──


async def test_list_plugins_groups(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query"])
    data = console.list_plugins()
    row = next(p for p in data["plugins"] if p["name"] == "erp-mcp")
    # 仅 ticked 工具桥接为插件（未 tick 的候选在 /mcp 工具清单里勾选）
    assert row["nodes"] == ["erp.query"]
    assert row["ticked"] == ["erp.query"]
    assert data["require_signing"] is False


async def test_tick_nodes_reconcile(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query", "erp.push"])
    out = await console.tick_nodes("erp-mcp", ["erp.query"])
    assert out["ticked"] == ["erp.query"]
    assert out["errors"] == []
    states = {s["plugin_id"]: s["state"] for s in console.lifecycle.list_states()}
    assert states["mcp__erp-mcp__erp.query"] == PluginState.ENABLED.value
    # 未加载过的节点 disable 是 no-op（无实例）—— 至少不能是 ENABLED
    assert states.get("mcp__erp-mcp__erp.push") != PluginState.ENABLED.value
    missing = await console.tick_nodes("ghost", [])
    assert missing["errors"] == ["plugin 'ghost' not found"]


async def test_remove_plugin(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query"])
    out = await console.remove_plugin("erp-mcp")
    assert out["removed"] is True
    assert console.registry.get("mcp__erp-mcp__erp.query") is None


async def test_install_plugin_declarative(console, tmp_path):
    pdir = tmp_path / "myplugin"
    pdir.mkdir()
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "id": "myplugin",
                "name": "My Plugin",
                "version": "1.2.0",
                "description": "demo",
                "category": "custom",
                "capabilities": ["mcp_tool"],
                "entry_point": "json:dump",
            }
        ),
        encoding="utf-8",
    )
    out = console.install_plugin(str(pdir))
    assert out["installed"] is True
    assert out["plugin"] == "myplugin"
    assert console.registry.get("myplugin").version == "1.2.0"
    # resync 重新注册自定义插件
    console.registry.unregister("myplugin")
    await console.resync()
    assert console.registry.get("myplugin")


def test_install_plugin_bad_source(console, tmp_path):
    out = console.install_plugin(str(tmp_path / "nope"))
    assert out["installed"] is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert console.install_plugin(str(empty))["installed"] is False
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.json").write_text('{"id": "x"}', encoding="utf-8")
    out = console.install_plugin(str(bad))
    assert "entry_point" in out["error"]


# ── resync ──


async def test_resync_rebuilds(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.install_connector(_conn_doc())
    await console.enable_connector("erp-json")
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query"])

    # 模拟重启：清掉运行时桥接，resync 从磁盘事实重建
    console.registry.unregister("connector__erp-json__erp.stock.fetch")
    console.registry.unregister("mcp__erp-mcp__erp.query")
    out = await console.resync()
    assert "connector.erp-json.erp.stock.fetch" in out["relisted"]
    assert "mcp.erp-mcp.erp.query" in out["relisted"]
    assert console.registry.get("connector__erp-json__erp.stock.fetch")
    assert console.registry.get("mcp__erp-mcp__erp.query")

    # disabled 连接器不重建
    await console.disable_connector("erp-json")
    console.registry.unregister("connector__erp-json__erp.stock.fetch")
    out = await console.resync()
    assert "connector.erp-json.erp.stock.fetch" not in out["relisted"]


# ── spine 派生 ──


async def test_spine_counts_by_source(console, mcp_server):
    console.save_credentials("mcp-cred", {"api_key": "good"})
    console.install_connector(_conn_doc())
    await console.enable_connector("erp-json")
    console.register_mcp_server("erp-mcp", mcp_server, "mcp-cred")
    await console.tick_mcp_tools("erp-mcp", ["erp.query"])
    caps = console.list_capabilities()
    by_src = {}
    for c in caps:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
    assert by_src == {"connector": 1, "mcp": 1}
