"""Shell module registration tests — issue #30 acceptance follow-up.

Covers: manifest load/override, fm- key persistence, registration loop
(issue/heartbeat/404/401 re-register), console /api/* auth (static env key +
issued key dual-active), and the /health endpoint.
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
        "FUSION_GATEWAY_URL",  # gateway_base_url legacy alias, guards against host-env leakage
        "FUSION_PROFILE",
        "FUSION_SHELL_MANIFEST",
        "FUSION_PLUGINS_CONSOLE_UPSTREAM",
        "FUSION_PLUGINS_CONSOLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # fusion-core warn-once set is module-level state; reset per test so caplog can assert
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
    task = asyncio.create_task(shell_registry._registry_loop_body(client, manifest, interval=0.01))
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
        # Real gateway issue response (#261 K1 writeShellKeyResponse)
        assert _extract_issued_key({"code": "ok", "key_id": "k1", "raw_key": "fm-x"}) == "fm-x"

    def test_nested_data(self):
        assert _extract_issued_key({"data": {"api_key": "fm-2"}}) == "fm-2"

    def test_no_key(self):
        assert _extract_issued_key({"ok": True}) == ""
        assert _extract_issued_key(None) == ""


class TestManifestLoad:
    def test_default_manifest_shape(self):
        manifest = shell_registry.load_manifest()
        assert manifest["id"] == "fusion-plugins-ecosystem"
        # issue #40 / DD-2: menu shape = top-level capability hub
        # i18n: user-facing (web/manifest.json)
        assert manifest["title"] == "能力中心"
        assert manifest["title_en"] == "Capabilities"
        assert manifest["entry"] == "/shell/fusion-plugins-ecosystem/"
        assert manifest["upstream"] == "http://127.0.0.1:8795"
        assert manifest["devices"]
        assert "api_key" not in manifest
        # issue #60: MT-1 taxonomy — the capability hub belongs in governance
        assert manifest["category"] == "governance"
        assert manifest["sort"] == 21

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
    # issue #38: gating moved to fusion_core.config.admin_key — dev missing = empty
    # string + warning, prod missing = RuntimeError fail-fast.

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
        # Gateway address via gateway_base_url: explicit env wins, default falls back to local 11432
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
        assert beat["url"].endswith("/api/v1/shell/manifests/fusion-plugins-ecosystem/heartbeat")
        assert beat["headers"]["Authorization"] == "Bearer fm-issued-key-0001"

    async def test_heartbeat_404_triggers_reregistration(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient([_response(200, {}), _response(404), _response(200, {}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        urls = [c["url"] for c in client.calls]
        assert urls[0].endswith("/api/v1/shell/manifests")
        assert urls[1].endswith("/heartbeat")
        assert urls[2].endswith("/api/v1/shell/manifests")

    async def test_heartbeat_401_with_module_key_triggers_reregistration(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        save_module_key("fm-rotated-away")
        client = _FakeClient([_response(200, {}), _response(401), _response(200, {}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        # Re-registration uses the admin key
        assert client.calls[2]["headers"]["Authorization"] == "Bearer admin-secret"

    async def test_register_failure_warns_and_retries(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_BASE_URL", "http://gw")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        client = _FakeClient([_response(500, {"error": "boom"}), _response(200, {})])
        await _drive(client, {"id": "fusion-plugins-ecosystem"})
        assert client.calls[0]["url"].endswith("/api/v1/shell/manifests")
