"""进程标题治理测试（issue #35）。"""

from __future__ import annotations

import sys
import types

from fusion_plugins_ecosystem.proctitle import (
    console_title,
    plugin_server_title,
    set_process_title,
)


def test_plugin_server_title_stdio() -> None:
    assert plugin_server_title("stdio", 0) == "fusion-plugin-server stdio"


def test_plugin_server_title_with_port() -> None:
    assert plugin_server_title("sse", 8765) == "fusion-plugin-server sse :8765"
    assert plugin_server_title("http", 8766) == "fusion-plugin-server http :8766"


def test_console_title() -> None:
    assert console_title(8795) == "fusion-plugin-console :8795"


def test_set_process_title_calls_setproctitle(monkeypatch) -> None:
    calls = []
    fake = types.ModuleType("setproctitle")
    fake.setproctitle = calls.append
    monkeypatch.setitem(sys.modules, "setproctitle", fake)
    assert set_process_title("fusion-plugin-server stdio") is True
    assert calls == ["fusion-plugin-server stdio"]


def test_set_process_title_missing_module_degrades(monkeypatch) -> None:
    # sys.modules 显式 None → import 抛 ImportError → 降级 False 不炸
    monkeypatch.setitem(sys.modules, "setproctitle", None)
    assert set_process_title("fusion-plugin-console :8795") is False


def test_set_process_title_import_error_degrades(monkeypatch) -> None:
    class _Boom(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError("broken setproctitle")

    monkeypatch.setitem(sys.modules, "setproctitle", _Boom("setproctitle"))
    assert set_process_title("x") is False
