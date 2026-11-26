"""issue #38 config resolution acceptance — fusion_core.config single-point resolution behavior.

Covers: DATA_DIR default/env override, service_port env override, and the
admin_key dev/prod semantics (low-level contract complementing the shell_registry gating).
"""

import importlib
from pathlib import Path

import pytest

import fusion_plugins_ecosystem.config as fpes_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "FUSION_PLUGINS_ECOSYSTEM_DATA_DIR",
        "FUSION_PLUGINS_ECOSYSTEM_PORT",
        "FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT",
        "FUSION_ADMIN_KEY",
        "FUSION_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    import fusion_core.config as _fc_config

    _fc_config._ANON_WARNED.clear()


class TestDataDir:
    def test_default_locks_existing_deploy_path(self):
        # Default pins the existing deployment path ~/.fusion-plugins-ecosystem (fm- keys are not migrated)
        assert fpes_config.DATA_DIR == Path.home() / ".fusion-plugins-ecosystem"

    def test_env_override(self, monkeypatch):
        with monkeypatch.context() as m:
            m.setenv("FUSION_PLUGINS_ECOSYSTEM_DATA_DIR", "/tmp/fpes-data")
            reloaded = importlib.reload(fpes_config)
            assert reloaded.DATA_DIR == Path("/tmp/fpes-data")
        # Reloading after removing the env restores the default (prevents polluting module state for later tests)
        importlib.reload(fpes_config)
        assert fpes_config.DATA_DIR == Path.home() / ".fusion-plugins-ecosystem"

    def test_consumers_share_data_dir(self):
        # Single point: web_module_key / extensions / connectors / marketplace share one root
        from fusion_plugins_ecosystem import (
            extensions_console,
            marketplace,
            rest_connector,
            shell_registry,
        )

        root = fpes_config.DATA_DIR
        assert shell_registry.MODULE_KEY_PATH == root / "web_module_key"
        assert Path(extensions_console.DEFAULT_EXTENSIONS_DIR) == root / "extensions"
        assert Path(rest_connector.DEFAULT_CONNECTOR_DIR) == root / "connectors"
        assert Path(marketplace.DEFAULT_INSTALL_ROOT) == root / "marketplace"


class TestServicePort:
    def test_console_default_8795(self):
        from fusion_plugins_ecosystem.console_app import DEFAULT_PORT

        assert DEFAULT_PORT == 8795

    def test_console_env_override(self, monkeypatch):
        from fusion_core.config import service_port

        monkeypatch.setenv("FUSION_PLUGINS_ECOSYSTEM_CONSOLE_PORT", "18795")
        assert service_port("plugins-ecosystem-console", default=8795) == 18795

    def test_server_env_override_and_default(self, monkeypatch):
        from fusion_core.config import service_port

        assert service_port("plugins-ecosystem", default=0) == 0
        monkeypatch.setenv("FUSION_PLUGINS_ECOSYSTEM_PORT", "8765")
        assert service_port("plugins-ecosystem", default=0) == 8765

    def test_invalid_port_raises(self, monkeypatch):
        from fusion_core.config import service_port

        monkeypatch.setenv("FUSION_PLUGINS_ECOSYSTEM_PORT", "not-a-port")
        with pytest.raises(ValueError, match="not an integer"):
            service_port("plugins-ecosystem", default=0)


class TestAdminKeySemantics:
    def test_dev_missing_key_empty_with_warning(self, caplog):
        from fusion_core.config import admin_key

        with caplog.at_level("WARNING"):
            assert admin_key() == ""
        assert "FUSION_ADMIN_KEY" in caplog.text

    def test_prod_missing_key_runtime_error(self, monkeypatch):
        from fusion_core.config import admin_key

        monkeypatch.setenv("FUSION_PROFILE", "prod")
        with pytest.raises(RuntimeError, match="FUSION_ADMIN_KEY"):
            admin_key()

    def test_explicit_key_passthrough(self, monkeypatch):
        from fusion_core.config import admin_key

        monkeypatch.setenv("FUSION_ADMIN_KEY", "admin-secret")
        assert admin_key() == "admin-secret"


class TestLegacyEnvCleanup:
    def test_no_local_resolve_helpers_remain(self):
        # issue #38 acceptance: this repo no longer has hand-written key resolution functions
        import subprocess

        result = subprocess.run(
            ["grep", "-rn", "def _resolve", "fusion_plugins_ecosystem/"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == ""

    def test_no_legacy_env_literals_in_source(self):
        # config-conventions §6: deprecated variables must no longer appear in product code
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rn",
                "-e",
                "FUSION_SHELL_GATEWAY_URL",
                "-e",
                "FUSION_GATEWAY_ADMIN_KEY",
                "fusion_plugins_ecosystem/",
            ],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == ""
