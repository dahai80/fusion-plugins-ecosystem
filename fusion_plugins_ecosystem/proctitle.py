# Process title governance (issue #35).
# Long-running processes show product name + role/port in ps/Activity
# Monitor instead of bare Python. Under macOS homebrew Python the argv
# title gets overridden by Python.app, so setproctitle is the only reliable
# means (same pattern as fusion-gupiao cli.py / fusion-k12-teacher cli.py).
# Silently degrades when setproctitle is missing, no functional impact.

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def set_process_title(title: str) -> bool:
    # Set the process title; returns False when setproctitle is unavailable.
    try:
        import setproctitle

        setproctitle.setproctitle(title)
        logger.debug("proctitle: process title set to %s", title)
        return True
    except Exception as exc:
        logger.debug("proctitle: setproctitle unavailable, skipping title set: %s", exc)
        return False


def plugin_server_title(transport: str, port: int) -> str:
    # fusion-plugin-server title: includes the transport type so stdio/sse/
    # http instances are distinguishable.
    if transport == "stdio":
        return "fusion-plugin-server stdio"
    return f"fusion-plugin-server {transport} :{port}"


def console_title(port: int) -> str:
    # fusion-plugin-console title.
    return f"fusion-plugin-console :{port}"
