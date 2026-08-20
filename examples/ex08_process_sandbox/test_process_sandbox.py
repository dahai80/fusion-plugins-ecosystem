"""Example 08 - tests for PROCESS sandbox isolation.

SandboxMode.PROCESS spawns a subprocess (PluginSandbox) and runs the entry
point there over stdin/stdout JSON IPC. The entry point must be a module-level
attribute importable as "module:attr". The result is returned via IPC.

Requires the project root on sys.path so the subprocess can import
examples.ex08_process_sandbox.process_plugin.
"""

from __future__ import annotations

import fusion_plugins_ecosystem as fpe
from fusion_plugins_ecosystem.desk_runtime import DeskRuntime

from examples.ex08_process_sandbox.process_plugin import ISOLATED_WORKER_MANIFEST


def _make_lifecycle() -> fpe.PluginLifecycle:
    registry = fpe.PluginRegistry(desk=DeskRuntime())
    registry.register(ISOLATED_WORKER_MANIFEST)
    return fpe.PluginLifecycle(registry)


async def test_process_sandbox_executes_in_subprocess():
    lifecycle = _make_lifecycle()
    await lifecycle.enable("isolated_worker")

    result = await lifecycle.execute("isolated_worker", {"value": "hello"})

    # entry reverses the string and tags it as subprocess-side
    assert result == {"processed": "olleh", "pid_side": "subprocess"}

    await lifecycle.disable("isolated_worker")
    lifecycle.unload("isolated_worker")


async def test_process_sandbox_empty_value():
    lifecycle = _make_lifecycle()
    await lifecycle.enable("isolated_worker")

    result = await lifecycle.execute("isolated_worker", {"value": ""})
    assert result == {"processed": "", "pid_side": "subprocess"}

    await lifecycle.disable("isolated_worker")
    lifecycle.unload("isolated_worker")
