"""插件沙箱：进程隔离执行。

PluginSandbox (宿主侧) 通过子进程运行插件，
SandboxWorker (子进程侧) 通过 stdin/stdout JSON 消息与宿主通信。

IPC 协议：
    宿主 → 子进程: {"type": "call", "id": "uuid", "method": "execute", "args": {...}}
    子进程 → 宿主: {"type": "result", "id": "uuid", "result": {...}}
    子进程 → 宿主: {"type": "heartbeat", "timestamp": 1234567890}
    子进程 → 宿主: {"type": "log", "level": "INFO", "message": "..."}
    子进程 → 宿主: {"type": "error", "id": "uuid", "error": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResourceLimits:
    """子进程资源限制。"""

    vram_budget_mb: int = 0
    memory_limit_mb: int = 512
    cpu_limit: float = 1.0
    timeout_seconds: int = 600
    grace_period_seconds: int = 10


class SandboxHealth(str, Enum):
    """沙箱健康状态。"""

    ALIVE = "alive"
    DEAD = "dead"
    TIMEOUT = "timeout"
    KILLED = "killed"


@dataclass
class SandboxProcess:
    """运行中的沙箱子进程信息。"""

    plugin_id: str
    process: asyncio.subprocess.Process
    limits: ResourceLimits
    health: SandboxHealth = SandboxHealth.ALIVE
    last_heartbeat: float = field(default_factory=time.time)
    pending_calls: dict[str, asyncio.Future] = field(default_factory=dict)
    reader_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None


class PluginSandbox:
    """插件沙箱管理器（宿主侧）。

    用法：
        sandbox = PluginSandbox()
        await sandbox.spawn("my_plugin", entry_point, config)
        result = await sandbox.call("my_plugin", "execute", {"input": "hello"})
        await sandbox.kill("my_plugin")
    """

    def __init__(self, default_limits: ResourceLimits | None = None) -> None:
        self._processes: dict[str, SandboxProcess] = {}
        self.default_limits = default_limits or ResourceLimits()

    async def spawn(
        self,
        plugin_id: str,
        entry_point: Any,
        config: dict[str, Any] | None = None,
        limits: ResourceLimits | None = None,
    ) -> None:
        """启动沙箱子进程。"""
        if plugin_id in self._processes:
            logger.warning("sandbox: plugin %s already spawned, killing old", plugin_id)
            await self.kill(plugin_id)

        res_limits = limits or self.default_limits
        worker_code = self._build_worker_script(entry_point, config or {}, res_limits)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                worker_code,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error("sandbox: failed to spawn %s: %s", plugin_id, e)
            raise

        sandbox_proc = SandboxProcess(
            plugin_id=plugin_id,
            process=proc,
            limits=res_limits,
        )

        sandbox_proc.reader_task = asyncio.create_task(self._read_loop(sandbox_proc))
        sandbox_proc.heartbeat_task = asyncio.create_task(
            self._heartbeat_monitor(sandbox_proc)
        )

        self._processes[plugin_id] = sandbox_proc
        logger.info("sandbox: spawned %s (pid=%d)", plugin_id, proc.pid)

    async def call(
        self, plugin_id: str, method: str, args: dict[str, Any] | None = None
    ) -> Any:
        """调用沙箱内插件方法。"""
        proc = self._processes.get(plugin_id)
        if proc is None:
            raise KeyError(f"sandbox: plugin {plugin_id!r} not spawned")
        if proc.health != SandboxHealth.ALIVE:
            raise RuntimeError(f"sandbox: plugin {plugin_id!r} is {proc.health.value}")

        call_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        proc.pending_calls[call_id] = future

        message = {
            "type": "call",
            "id": call_id,
            "method": method,
            "args": args or {},
        }

        try:
            line = json.dumps(message) + "\n"
            proc.process.stdin.write(line.encode("utf-8"))
            await proc.process.stdin.drain()
        except Exception as e:
            proc.pending_calls.pop(call_id, None)
            future.set_exception(e)

        return await future

    async def kill(self, plugin_id: str) -> None:
        """终止沙箱子进程。"""
        proc = self._processes.pop(plugin_id, None)
        if proc is None:
            return

        proc.health = SandboxHealth.KILLED
        if proc.reader_task and not proc.reader_task.done():
            proc.reader_task.cancel()
        if proc.heartbeat_task and not proc.heartbeat_task.done():
            proc.heartbeat_task.cancel()

        try:
            proc.process.terminate()
            try:
                await asyncio.wait_for(
                    proc.process.wait(), timeout=proc.limits.grace_period_seconds
                )
            except asyncio.TimeoutError:
                proc.process.kill()
                await proc.process.wait()
        except ProcessLookupError:
            pass

        logger.info("sandbox: killed %s", plugin_id)

    def health(self, plugin_id: str) -> SandboxHealth:
        """查询沙箱健康状态。"""
        proc = self._processes.get(plugin_id)
        if proc is None:
            return SandboxHealth.DEAD
        return proc.health

    async def shutdown_all(self) -> None:
        """关闭所有沙箱子进程。"""
        for plugin_id in list(self._processes.keys()):
            await self.kill(plugin_id)

    async def _read_loop(self, proc: SandboxProcess) -> None:
        """读取子进程 stdout 消息。"""
        try:
            while proc.health == SandboxHealth.ALIVE:
                line = await proc.process.stdout.readline()
                if not line:
                    proc.health = SandboxHealth.DEAD
                    logger.warning("sandbox: %s stdout EOF", proc.plugin_id)
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "sandbox: %s invalid JSON: %s", proc.plugin_id, text[:100]
                    )
                    continue

                msg_type = msg.get("type")
                if msg_type == "result":
                    await self._handle_result(proc, msg)
                elif msg_type == "error":
                    await self._handle_error(proc, msg)
                elif msg_type == "heartbeat":
                    proc.last_heartbeat = time.time()
                elif msg_type == "log":
                    self._handle_log(proc, msg)
                else:
                    logger.warning(
                        "sandbox: unknown msg type %r from %s", msg_type, proc.plugin_id
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("sandbox: %s read error: %s", proc.plugin_id, e)
            proc.health = SandboxHealth.DEAD

    async def _heartbeat_monitor(self, proc: SandboxProcess) -> None:
        """监控子进程心跳。"""
        try:
            while proc.health == SandboxHealth.ALIVE:
                await asyncio.sleep(proc.limits.timeout_seconds / 4)
                if proc.health != SandboxHealth.ALIVE:
                    break
                elapsed = time.time() - proc.last_heartbeat
                if elapsed > proc.limits.timeout_seconds:
                    proc.health = SandboxHealth.TIMEOUT
                    logger.warning(
                        "sandbox: %s heartbeat timeout (%.0fs)", proc.plugin_id, elapsed
                    )
                    await self.kill(proc.plugin_id)
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_result(self, proc: SandboxProcess, msg: dict) -> None:
        call_id = msg.get("id", "")
        future = proc.pending_calls.pop(call_id, None)
        if future and not future.done():
            future.set_result(msg.get("result"))

    async def _handle_error(self, proc: SandboxProcess, msg: dict) -> None:
        call_id = msg.get("id", "")
        future = proc.pending_calls.pop(call_id, None)
        error_msg = msg.get("error", "Unknown sandbox error")
        if future and not future.done():
            future.set_exception(RuntimeError(str(error_msg)))

    def _handle_log(self, proc: SandboxProcess, msg: dict) -> None:
        level = msg.get("level", "INFO")
        message = msg.get("message", "")
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("sandbox:%s: %s", proc.plugin_id, message)

    def _build_worker_script(
        self, entry_point: Any, config: dict, limits: ResourceLimits
    ) -> str:
        """构建子进程 worker 脚本。"""
        entry_str = ""
        if callable(entry_point):
            mod = getattr(entry_point, "__module__", "")
            qualname = getattr(entry_point, "__qualname__", "")
            if mod:
                entry_str = f"{mod}:{qualname}"
        elif isinstance(entry_point, str):
            entry_str = entry_point

        return (
            "import sys,json,time,importlib,resource,logging\n"
            "logging.basicConfig(level=logging.INFO,stream=sys.stderr,"
            "format='%(name)s %(levelname)s: %(message)s')\n"
            f"_ENTRY={entry_str!r}\n"
            f"_CONFIG={json.dumps(config)}\n"
            f"_MEM_LIMIT_MB={limits.memory_limit_mb}\n"
            "logger=logging.getLogger('sandbox.worker')\n"
            "try:\n"
            "    soft,hard=resource.getrlimit(resource.RLIMIT_AS)\n"
            "    resource.setrlimit(resource.RLIMIT_AS,(_MEM_LIMIT_MB*1024*1024,hard))\n"
            "except Exception as e:\n"
            "    logger.warning('setrlimit failed: %s',e)\n"
            "def _load_entry():\n"
            "    if not _ENTRY: return None\n"
            "    if ':' in _ENTRY:\n"
            "        mod_path,_,attr=_ENTRY.partition(':')\n"
            "        mod=importlib.import_module(mod_path)\n"
            "        return getattr(mod,attr) if attr else mod\n"
            "    return importlib.import_module(_ENTRY)\n"
            "_entry_obj=_load_entry()\n"
            "def _send(msg):\n"
            "    sys.stdout.write(json.dumps(msg)+'\\n')\n"
            "    sys.stdout.flush()\n"
            "def _heartbeat_loop():\n"
            "    import threading\n"
            "    def _tick():\n"
            "        while True:\n"
            "            time.sleep(30)\n"
            "            _send({'type':'heartbeat','timestamp':time.time()})\n"
            "    t=threading.Thread(target=_tick,daemon=True)\n"
            "    t.start()\n"
            "_heartbeat_loop()\n"
            "for line in sys.stdin:\n"
            "    line=line.strip()\n"
            "    if not line: continue\n"
            "    try:\n"
            "        req=json.loads(line)\n"
            "    except json.JSONDecodeError:\n"
            "        continue\n"
            "    if req.get('type')!='call': continue\n"
            "    call_id=req.get('id','')\n"
            "    method=req.get('method','')\n"
            "    args=req.get('args',{})\n"
            "    try:\n"
            "        if method=='execute' and _entry_obj:\n"
            "            if hasattr(_entry_obj,'__self__'): result=_entry_obj(args)\n"
            "            elif callable(_entry_obj): result=_entry_obj(_CONFIG,args)\n"
            "            else: result=str(_entry_obj)\n"
            "            _send({'type':'result','id':call_id,'result':result})\n"
            "        else:\n"
            "            _send({'type':'error','id':call_id,'error':f'Unknown method {method}'})\n"
            "    except Exception as e:\n"
            "        _send({'type':'error','id':call_id,'error':str(e)})\n"
        )
