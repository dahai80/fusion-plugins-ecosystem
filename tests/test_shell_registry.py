"""shell 模块注册测试 — issue #30 验收补充。

覆盖: manifest 加载/覆盖、fm- 密钥持久化、注册循环 (签发/心跳/404/401 重注册)、
console /api/* 鉴权 (静态 env key + 签发 key 双活)、/health 端点。
"""

import asyncio
import json
import stat
from unittest.mock import MagicMock

import pytest

from fusion_plugins_ecosystem import shell_registry
from fusion_plugins_ecosystem.shell_registry import (
    _extract_issued_key,
    _mask_key,
    load_module_key,
    save_module_key,
)


@pytest.fixture(autouse=True)
def _key_path(tmp_path, monkeypatch):
    fake = tmp_path / "web_module_key"
    monkeypatch.setattr(shell_registry, "MODULE_KEY_PATH", fake)
    yield fake


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "FUSION_ADMIN_KEY",
        "FUSION_GATEWAY_BASE_URL",
        "FUSION_GATEWAY_URL",  # gateway_base_url legacy 别名，防宿主环境泄漏
        "FUSION_PROFILE",
        "FUSION_SHELL_MANIFEST",
        "FUSION_PLUGINS_CONSOLE_UPSTREAM",
        "FUSION_PLUGINS_CONSOLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # fusion-core warn-once 集合是模块级状态，逐用例复位保证 caplog 可断言
    import fusion_core.config as _fc_config

    _fc_config._ANON_WARNED.clear()


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    payload = payload if payload is not None else {}
    resp.text = json.dumps(payload)
    resp.json = lambda: payload
    return resp


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0) if self._responses else _response(200, {})


async def _drive(client, manifest, rounds=0.08):
    task = asyncio.create_task(
        shell_registry._registry_loop_body(client, manifest, interval=0.01)
    )
    await asyncio.sleep(rounds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestModuleKeyPersistence:
    def test_save_and_load_roundtrip(self):
        assert load_module_key() == ""
        assert save_module_key("fm-fusion-plugins-ecosystem-abc123") is True
        assert load_module_key() == "fm-fusion-plugins-ecosystem-abc123"

    def test_file_mode_0600(self, _key_path):
        save_module_key("fm-x")
        assert stat.S_IMODE(_key_path.stat().st_mode) == 0o600

    def test_empty_key_not_saved(self, _key_path):
        assert save_module_key("") is False
        assert not _key_path.exists()

    def test_mask_key_never_plaintext(self):
        masked = _mask_key("fm-fusion-plugins-abcdef12")
        assert "abcdef12" not in masked
        assert masked == "fm-f****ef12"

    def test_load_missing_file_returns_empty(self):
        assert load_module_key() == ""


class TestIssuedKeyExtraction:
    def test_top_level_fields(self):
        for field in ("raw_key", "api_key", "issued_key", "key", "module_key"):
            assert _extract_issued_key({field: "fm-1"}) == "fm-1"

    def test_gateway_writeShellKeyResponse_shape(self):
        # 真实网关签发响应 (#261 K1 writeShellKeyResponse)
        assert (
            _extract_issued_key({"code": "ok", "key_id": "k1", "raw_key": "fm-x"})
            == "fm-x"
        )

    def test_nested_data(self):
        assert _extract_issued_key({"data": {"api_key": "fm-2"}}) == "fm-2"

    def test_no_key(self):
        assert _extract_issued_key({"ok": True}) == ""
        assert _extract_issued_key(None) == ""


class TestManifestLoad:
    def test_default_manifest_shape(self):
        manifest = shell_registry.load_manifest()
        assert manifest["id"] == "fusion-plugins-ecosystem"
        assert manifest["entry"] == "/shell/fusion-plugins-ecosystem/"
        assert manifest["upstream"] == "http://127.0.0.1:8795"
        assert manifest["devices"]
        assert "api_key" not in manifest

    def test_upstream_override_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_UPSTREAM", "http://127.0.0.1:18995")
        manifest = shell_registry.load_manifest()
        assert manifest["upstream"] == "http://127.0.0.1:18995"

    def test_static_key_kept_when_env_set(self, monkeypatch):
        monkeypatch.setenv("FUSION_PLUGINS_CONSOLE_API_KEY", "static-secret")
        manifest = shell_registry.load_manifest()
        assert manifest.get("api_key") == "static-secret"

    def test_missing_manifest_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_SHELL_MANIFEST", "/nonexistent/manifest.json")
        with pytest.raises(FileNotFoundError):
            shell_registry.load_manifest()


class TestRegistrationGating:
    # issue #38：门控迁 fusion_core.config.admin_key — dev 缺失=空串+告警，
    # prod 缺失=RuntimeError fail-fast。

    def test_disabled_without_env(self, caplog):
        with caplog.at_level("WARNING"):
            assert shell_registry.shell_registration_enabled() is False
        assert "FUSION_ADMIN_KEY" in caplog.text

    def test_enabled_with_admin_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        assert shell_registry.shell_registration_enabled() is True

    def test_prod_missing_admin_key_fail_fast(self, monkeypatch):
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        with pytest.raises(RuntimeError, match="FUSION_ADMIN_KEY"):
            shell_registry.shell_registration_enabled()

    def test_prod_with_admin_key_enabled(self, monkeypatch):
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        assert shell_registry.shell_registration_enabled() is True

    def test_gateway_url_single_point(self, monkeypatch):
        # 网关地址走 gateway_base_url：显式 env 优先，缺省回落本机 11432
        from fusion_core.config import gateway_base_url

        assert gateway_base_url() == "http://127.0.0.1:11432"
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw:9000")
        assert gateway_base_url() == "http://gw:9000"


class TestRegistryLoop:
    async def test_registration_persists_issued_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        issued = {
            "code": "ok",
            "key_id": "k1",
            "raw_key": "fm-fusion-plugins-deadbeef01",
        }
        client = _FakeClient([_response(200, issued), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        assert load_module_key() == "fm-fusion-plugins-deadbeef01"
        assert client.calls[0]["headers"]["Authorization"] == "Bearer admin-secret"
        assert client.calls[0]["url"] == "http://gw/api/v1/shell/manifests"

    async def test_no_key_in_response_nothing_persisted(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient([_response(200, {"ok": True}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        assert load_module_key() == ""

    async def test_heartbeat_uses_issued_key(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        save_module_key("fm-issued-key-0001")
        client = _FakeClient([_response(200, {}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        beat = client.calls[1]
        assert beat["url"].endswith(
            "/api/v1/shell/manifests/fusion-plugins-ecosystem/heartbeat"
        )
        assert beat["headers"]["Authorization"] == "Bearer fm-issued-key-0001"

    async def test_heartbeat_404_triggers_reregistration(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient(
            [_response(200, {}), _response(404), _response(200, {}), _response(200, {})]
        )
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        urls = [c["url"] for c in client.calls]
        assert urls[0].endswith("/api/v1/shell/manifests")
        assert urls[1].endswith("/heartbeat")
        assert urls[2].endswith("/api/v1/shell/manifests")

    async def test_heartbeat_401_with_module_key_triggers_reregistration(
        self, monkeypatch
    ):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        save_module_key("fm-rotated-away")
        client = _FakeClient(
            [_response(200, {}), _response(401), _response(200, {}), _response(200, {})]
        )
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        # 重注册走 admin key
        assert client.calls[2]["headers"]["Authorization"] == "Bearer admin-secret"

    async def test_register_failure_warns_and_retries(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient([_response(500, {"error": "boom"}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        assert client.calls[0]["url"].endswith("/api/v1/shell/manifests")
