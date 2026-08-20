"""Example 08 - process sandbox isolation.

SandboxMode.PROCESS runs the entry point in a subprocess via PluginSandbox.
The entry point MUST be a module-level attribute (importable) — closures and
lambdas fail. Here entry_point is the "module:attr" string
"examples.ex08_process_sandbox.process_plugin:isolated_worker".

Note: PROCESS sandbox CANNOT acquire VRAM (raises NotImplementedError),
so no VRAM_CONSUMER here.
"""

from __future__ import annotations

import logging

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType, SandboxMode

logger = logging.getLogger(__name__)


def isolated_worker(desk, params):
    value = params.get("value", "")
    if desk is not None:
        desk.log("isolated_worker", "INFO", "ran in subprocess", value=value)
    return {"processed": value[::-1], "pid_side": "subprocess"}


ISOLATED_WORKER_MANIFEST = PluginManifest(
    id="isolated_worker",
    name="Isolated Worker",
    version="0.1.0",
    category=PluginCategory.CUSTOM,
    description="Runs in a PROCESS sandbox subprocess (crash-isolated).",
    capabilities=(PluginCapability.MCP_TOOL,),
    params=(
        PluginParam(
            name="value",
            type=PluginParamType.STRING,
            description="String to reverse in the subprocess.",
            required=True,
        ),
    ),
    entry_point="examples.ex08_process_sandbox.process_plugin:isolated_worker",
    timeout_seconds=60,
    sandbox_mode=SandboxMode.PROCESS,
)
