"""进程标题治理（issue #35）。

长驻进程在 ps/活动监视器中显示产品名 + 角色/端口，而非裸 Python。
macOS homebrew Python 下 argv 标题会被 Python.app 覆盖，setproctitle 是
唯一可靠手段（同 fusion-gupiao cli.py / fusion-k12-teacher cli.py 模式）。
setproctitle 缺失时静默降级，不影响功能。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def set_process_title(title: str) -> bool:
    """设置进程标题；setproctitle 不可用时降级返回 False。"""
    try:
        import setproctitle

        setproctitle.setproctitle(title)
        logger.debug("proctitle: 进程标题已设置为 %s", title)
        return True
    except Exception as exc:
        logger.debug("proctitle: setproctitle 不可用，跳过进程标题设置: %s", exc)
        return False


def plugin_server_title(transport: str, port: int) -> str:
    """fusion-plugin-server 标题：含传输类型（stdio/sse/http 实例可区分）。"""
    if transport == "stdio":
        return "fusion-plugin-server stdio"
    return f"fusion-plugin-server {transport} :{port}"


def console_title(port: int) -> str:
    """fusion-plugin-console 标题。"""
    return f"fusion-plugin-console :{port}"
