"""rest_connector 测试：校验 / 通道拆分 / 持久化 / 执行契约。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fusion_plugins_ecosystem.rest_connector as rc


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_PLUGIN_CONNECTOR_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("FUSION_PLUGIN_EXTENSIONS_DIR", str(tmp_path / "extensions"))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
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
            self._json(200, {"rows": [{"sku": "A1"}, {"sku": "A2"}], "total": 2})
        else:
            self._json(404, {"error": "no route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        if self.path.startswith("/hook"):
            self._json(200, {"received": json.loads(body or b"{}").get("x", None)})
        else:
            self._json(404, {"error": "no route"})


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _doc(base_url):
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
                "response": {"rows": "$.rows", "total": "$.total"},
            },
            "erp.order.push": {
                "method": "POST",
                "path": "/orders",
            },
        },
    }


# ── 静态校验 ──


def test_validate_ok():
    assert rc.validate_connector(_doc("https://x.example.com")) == []


def test_validate_missing_id():
    problems = rc.validate_connector(
        {"kind": "rest", "base_url": "https://x", "operations": {"a": {"path": "/"}}}
    )
    assert "missing id" in problems


def test_validate_bad_kind():
    doc = _doc("https://x")
    doc["kind"] = "graphql"
    assert any("kind" in p for p in rc.validate_connector(doc))


def test_validate_rest_requires_base_url():
    doc = _doc("https://x")
    doc.pop("base_url")
    assert any("base_url" in p for p in rc.validate_connector(doc))


def test_validate_operations_required():
    problems = rc.validate_connector(
        {"id": "x", "kind": "rest", "base_url": "https://x"}
    )
    assert any("operations" in p for p in problems)


def test_validate_bad_method_and_response_path():
    doc = _doc("https://x")
    doc["operations"]["a"] = {
        "method": "FETCH",
        "path": "/a",
        "response": {"r": "rows"},
    }
    problems = rc.validate_connector(doc)
    assert any("invalid method" in p for p in problems)
    assert any("JSONPath-lite" in p for p in problems)


def test_validate_auth_requires_credential_fields():
    doc = _doc("https://x")
    doc["auth"] = {"type": "bearer"}
    assert any("credential_fields" in p for p in rc.validate_connector(doc))


# ── 通道拆分 ──


def test_read_write_channels():
    doc = _doc("https://x")
    reads = rc.read_channels(doc)
    assert [c["id"] for c in reads] == ["erp.stock.fetch"]
    assert rc.write_channels(doc) == ["erp.order.push"]


# ── 持久化 ──


def test_save_get_list_delete_roundtrip():
    doc = _doc("https://x")
    rc.save_connector("erp-json", doc)
    assert rc.get_connector("erp-json") == doc
    listing = rc.list_connectors()
    assert listing[0]["id"] == "erp-json"
    report = rc.connector_report()
    assert report[0]["problems"] == []
    assert rc.delete_connector("erp-json") is True
    assert rc.get_connector("erp-json") is None
    assert rc.delete_connector("erp-json") is False


def test_get_unreadable_file(tmp_path):
    d = tmp_path / "connectors"
    d.mkdir()
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    assert rc.get_connector("bad") is None


# ── 执行契约 ──


async def test_execute_rest_ok_with_extraction(server):
    doc = _doc(server)
    out = await rc.execute_operation(
        doc, "erp.stock.fetch", credentials={"token": "tok-1"}
    )
    assert out["ok"] is True
    assert out["status"] == 200
    assert out["extracted"]["total"] == 2
    assert len(out["extracted"]["rows"]) == 2
    assert out["duration_ms"] >= 0


async def test_execute_missing_credentials(server):
    doc = _doc(server)
    doc["auth"] = {"type": "bearer", "credential_fields": ["token"]}
    out = await rc.execute_operation(doc, "erp.stock.fetch")
    assert out["ok"] is False
    assert out["error_code"] == "missing_credentials"
    assert "token" in out["reason"]


async def test_execute_unknown_operation(server):
    out = await rc.execute_operation(_doc(server), "nope")
    assert out == {
        "ok": False,
        "error_code": "unknown_operation",
        "reason": "operation 'nope' not defined",
    }


async def test_execute_http_error_with_code_map(server):
    doc = _doc(server)
    doc["error_code_map"] = {"unauthorized": "auth failed"}
    out = await rc.execute_operation(
        doc, "erp.stock.fetch", credentials={"token": "wrong"}
    )
    assert out["ok"] is False
    assert out["status"] == 401
    assert out["error_code"] == "unauthorized"
    assert out["reason"] == "auth failed"


async def test_execute_webhook_delivery(server):
    doc = {
        "id": "wh",
        "kind": "webhook",
        "base_url": server,
        "operations": {"notify": {"method": "POST", "path": "/hook"}},
    }
    out = await rc.execute_operation(doc, "notify", payload={"x": 1})
    assert out["ok"] is True
    assert out["data"] == {"delivered": True}


async def test_execute_db_not_implemented():
    doc = {
        "id": "db1",
        "kind": "db",
        "operations": {"q": {"method": "GET", "path": "/"}},
    }
    out = await rc.execute_operation(doc, "q")
    assert out["error_code"] == "not_implemented"


async def test_execute_unknown_kind():
    doc = {
        "id": "x",
        "kind": "grpc",
        "operations": {"q": {"method": "GET", "path": "/"}},
    }
    out = await rc.execute_operation(doc, "q")
    assert out["error_code"] == "unknown_kind"


# ── JSONPath-lite ──


def test_extract_nested_and_list():
    data = {"a": {"b": [{"id": 1}, {"id": 2}]}}
    assert rc._extract(data, "$.a.b.1.id") == 2
    assert rc._extract(data, "$.a.b.9.id") is None
    assert rc._extract(data, "$.missing") is None
    assert rc._extract(data, "no-dollar") is None


def test_render_variables():
    assert rc._render("/orders/{since}", {"since": 7}) == "/orders/7"
    assert rc._render("/orders", {}) == "/orders"
