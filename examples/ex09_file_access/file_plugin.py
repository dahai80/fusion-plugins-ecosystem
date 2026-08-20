"""Example 09 - file access permission.

FILE_ACCESS capability. Checks desk.check_file_permission(plugin_id, path)
before reading. Empty allowed_paths allowlist = allow all; grant_permission
restricts to a list. The test grants a temp dir, asserts allowed/denied.
"""

from __future__ import annotations

import logging
import os

from fusion_plugins_ecosystem.registry import (
    PluginCapability,
    PluginCategory,
    PluginManifest,
    PluginParam,
)
from fusion_plugins_ecosystem.schema import PluginParamType

logger = logging.getLogger(__name__)


def read_file_plugin(desk, params):
    path = params.get("path", "")
    if desk is None:
        return {"error": "desk unavailable"}
    allowed = desk.check_file_permission("read_file_plugin", path)
    if not allowed:
        desk.log("read_file_plugin", "WARN", "permission denied", path=path)
        return {"error": "permission denied", "path": path}
    if not os.path.isfile(path):
        desk.log("read_file_plugin", "WARN", "not a file", path=path)
        return {"error": "not a file", "path": path}
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    desk.log("read_file_plugin", "INFO", "read", path=path, chars=len(content))
    return {"path": path, "content": content, "chars": len(content)}


READ_FILE_MANIFEST = PluginManifest(
    id="read_file_plugin",
    name="Read File",
    version="0.1.0",
    category=PluginCategory.FILE_INDEX,
    description="Reads a file after checking desk.check_file_permission.",
    capabilities=(PluginCapability.MCP_TOOL, PluginCapability.FILE_ACCESS),
    params=(
        PluginParam(
            name="path",
            type=PluginParamType.STRING,
            description="Absolute path to read.",
            required=True,
        ),
    ),
    entry_point=read_file_plugin,
    timeout_seconds=30,
)
