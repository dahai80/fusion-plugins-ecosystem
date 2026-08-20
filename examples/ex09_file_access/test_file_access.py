"""Example 09 - tests for FILE_ACCESS permission gating.

The plugin calls desk.check_file_permission(plugin_id, path) before reading.
- Empty allowed_paths → allow all (test convenience).
- grant_permission(plugin_id, [dirs]) → restricts to those paths (prefix match).

Tests grant a temp dir, assert an in-dir file reads and an outside file is
denied. Process data (temp files) cleaned up in teardown.
"""

from __future__ import annotations

import os
import tempfile

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

from examples.ex09_file_access.file_plugin import READ_FILE_MANIFEST


def _make_lifecycle(desk: DeskRuntime) -> fpe.PluginLifecycle:
    registry = fpe.PluginRegistry(desk=desk)
    registry.register(READ_FILE_MANIFEST)
    return fpe.PluginLifecycle(registry)


async def test_allowed_file_reads():
    with tempfile.TemporaryDirectory() as tmp:
        desk = DeskRuntime()
        desk.grant_permission("read_file_plugin", [tmp])
        lifecycle = _make_lifecycle(desk)

        target = os.path.join(tmp, "note.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("hello world")

        await lifecycle.enable("read_file_plugin")
        result = await lifecycle.execute("read_file_plugin", {"path": target})

        assert result["content"] == "hello world"
        assert result["chars"] == 11

        await lifecycle.disable("read_file_plugin")
        lifecycle.unload("read_file_plugin")


async def test_outside_file_denied():
    with tempfile.TemporaryDirectory() as inside:
        with tempfile.TemporaryDirectory() as outside:
            desk = DeskRuntime()
            desk.grant_permission("read_file_plugin", [inside])
            lifecycle = _make_lifecycle(desk)

            target = os.path.join(outside, "secret.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("nope")

            await lifecycle.enable("read_file_plugin")
            result = await lifecycle.execute("read_file_plugin", {"path": target})

            assert result["error"] == "permission denied"
            assert result["path"] == target

            await lifecycle.disable("read_file_plugin")
            lifecycle.unload("read_file_plugin")


async def test_empty_allowlist_allows_all():
    with tempfile.TemporaryDirectory() as tmp:
        # no grant_permission call → empty allowlist → allow all
        desk = DeskRuntime()
        lifecycle = _make_lifecycle(desk)

        target = os.path.join(tmp, "any.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("ok")

        await lifecycle.enable("read_file_plugin")
        result = await lifecycle.execute("read_file_plugin", {"path": target})

        assert result["content"] == "ok"

        await lifecycle.disable("read_file_plugin")
        lifecycle.unload("read_file_plugin")
