# Plugin sandbox: process-isolated execution.
#
# PluginSandbox (host side) runs plugins in subprocesses; SandboxWorker
# (subprocess side) talks to the host via stdin/stdout JSON messages.
#
# IPC protocol:
#     host → subprocess: {"type": "call", "id": "uuid", "method": "execute", "args": {...}}
#     subprocess → host: {"type": "result", "id": "uuid", "result": {...}}
#     subprocess → host: {"type": "heartbeat", "timestamp": 1234567890}
#     subprocess → host: {"type": "log", "level": "INFO", "message": "..."}
#     subprocess → host: {"type": "error", "id": "uuid", "error": "..."}

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

# P0-2: upper bound for waiting to reap after SIGKILL. Past this, give up
# waiting (the process may be in uninterruptible D-state) to avoid an
# unbounded await hanging the whole call chain.
_KILL_REAP_TIMEOUT = 5

# P1-8/E6: log level whitelist, prevents a worker sending an invalid level
# string from resolving to a non-log method via getattr.
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True)
class ResourceLimits:
    # Resource limits for the subprocess.

    vram_budget_mb: int = 0
    memory_limit_mb: int = 512
    # P3-5: default 0 = unlimited. RLIMIT_CPU counts cumulative CPU seconds;
    # 1.0 would SIGXCPU-kill legitimate compute plugins (MLX inference /
    # compression). Wall-clock caps are already covered by timeout_seconds +
    # heartbeat; plugins needing a CPU cap declare cpu_limit explicitly in
    # the manifest. The worker script only setrlimits when cpu_limit > 0.
    cpu_limit: float = 0.0
    timeout_seconds: int = 600
    grace_period_seconds: int = 10


class SandboxHealth(str, Enum):
    # Sandbox health states.

    ALIVE = "alive"
    DEAD = "dead"
    TIMEOUT = "timeout"
    KILLED = "killed"


@dataclass
class SandboxProcess:
    # Info about a running sandbox subprocess.

    plugin_id: str
    process: asyncio.subprocess.Process
    limits: ResourceLimits
    health: SandboxHealth = SandboxHealth.ALIVE
    last_heartbeat: float = field(default_factory=time.time)
    pending_calls: dict[str, asyncio.Future] = field(default_factory=dict)
    reader_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None


class PluginSandbox:
    # Plugin sandbox manager (host side).
    #
    # Usage:
    #     sandbox = PluginSandbox()
    #     await sandbox.spawn("my_plugin", entry_point, config)
    #     result = await sandbox.call("my_plugin", "execute", {"input": "hello"})
    #     await sandbox.kill("my_plugin")

    def __init__(
        self,
        default_limits: ResourceLimits | None = None,
        desk: Any = None,
    ) -> None:
        self._processes: dict[str, SandboxProcess] = {}
        self.default_limits = default_limits or ResourceLimits()
        # R1: host DeskRuntime handle, used for worker resource-proxy RPC callbacks
        self.desk = desk

    async def _handle_rpc(self, proc: SandboxProcess, msg: dict) -> None:
        # Handle resource requests where the worker calls back to the host
        # (R1: PROCESS sandbox resource proxy).
        #
        # The worker-side _DeskProxy's acquire_vram/mlx_chat/
        # check_file_permission/get_api_key send rpc messages back to the
        # host via stdout; the host executes them and writes the result
        # back over stdin.
        rpc_id = msg.get("id", "")
        method = msg.get("method", "")
        args = msg.get("args", {})
        result: Any = None
        error: str | None = None
        try:
            if self.desk is None:
                raise RuntimeError(
                    "host DeskRuntime not injected, cannot proxy resource requests"
                )
            if method == "acquire_vram":
                result = self.desk.acquire_vram(
                    args.get("plugin_id", proc.plugin_id),
                    int(args.get("mb", 0)),
                )
            elif method == "release_vram":
                self.desk.release_vram(args.get("plugin_id", proc.plugin_id))
                result = True
            elif method == "check_file_permission":
                result = self.desk.check_file_permission(
                    args.get("plugin_id", proc.plugin_id),
                    args.get("path", ""),
                )
            elif method == "get_api_key":
                result = self.desk.get_api_key(args.get("provider", ""))
            elif method == "mlx_chat":
                # Async method, needs await
                result = await self.desk.mlx_chat(
                    args.get("model", ""),
                    args.get("messages", []),
                    **args.get("kwargs", {}),
                )
            else:
                raise RuntimeError(f"unknown rpc method {method!r}")
        except Exception as exc:
            error = str(exc)
            logger.warning("sandbox: rpc %s failed: %s", method, exc)
        reply = {"type": "rpc_result", "id": rpc_id}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        try:
            line = json.dumps(reply, default=str) + "\n"
            proc.process.stdin.write(line.encode("utf-8"))
            await proc.process.stdin.drain()
        except Exception as exc:
            logger.warning("sandbox: rpc reply write failed: %s", exc)

    async def spawn(
        self,
        plugin_id: str,
        entry_point: Any,
        config: dict[str, Any] | None = None,
        limits: ResourceLimits | None = None,
    ) -> None:
        # Spawn the sandbox subprocess.
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
        sandbox_proc.stderr_task = asyncio.create_task(self._drain_stderr(sandbox_proc))

        self._processes[plugin_id] = sandbox_proc
        logger.info("sandbox: spawned %s (pid=%d)", plugin_id, proc.pid)
        if self.desk is not None:
            self.desk.metrics.counter(
                "sandbox_spawns_total", "total PROCESS sandbox subprocess spawns"
            ).inc(plugin=plugin_id, status="spawned")

    async def call(
        self, plugin_id: str, method: str, args: dict[str, Any] | None = None
    ) -> Any:
        # Call a plugin method inside the sandbox.
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

        try:
            return await asyncio.wait_for(future, timeout=proc.limits.timeout_seconds)
        except TimeoutError:
            proc.pending_calls.pop(call_id, None)
            # The worker must be killed after a timeout: otherwise an orphan
            # process lingers and the next call reuses the stuck process
            # (P0-4). _invoke_sandbox respawns when health != alive.
            self.desk_log(
                proc, "ERROR", f"call {call_id} timed out, killing worker and waiting for respawn"
            )
            await self.kill(plugin_id)
            raise TimeoutError(
                f"sandbox: plugin {plugin_id!r} call timed out after "
                f"{proc.limits.timeout_seconds}s"
            ) from None

    def desk_log(
        self, proc: SandboxProcess, level: str, message: str, **kw: Any
    ) -> None:
        # Sandbox-side logs go through the host logger (worker's own logs
        # are already relayed via IPC).
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("sandbox:%s: %s %s", proc.plugin_id, message, kw or "")

    async def kill(self, plugin_id: str) -> None:
        # Terminate the sandbox subprocess.
        proc = self._processes.pop(plugin_id, None)
        if proc is None:
            return

        proc.health = SandboxHealth.KILLED
        if proc.reader_task and not proc.reader_task.done():
            proc.reader_task.cancel()
        if proc.heartbeat_task and not proc.heartbeat_task.done():
            proc.heartbeat_task.cancel()
        if proc.stderr_task and not proc.stderr_task.done():
            proc.stderr_task.cancel()
        self._fail_pending(
            proc, RuntimeError(f"sandbox: plugin {plugin_id!r} was killed")
        )

        try:
            proc.process.terminate()
            try:
                await asyncio.wait_for(
                    proc.process.wait(), timeout=proc.limits.grace_period_seconds
                )
            except TimeoutError:
                proc.process.kill()
                # P0-2: bound the post-SIGKILL reap wait so this await never
                # hangs forever if the worker enters uninterruptible
                # D-state, taking down execute/_watch_loop with it.
                try:
                    await asyncio.wait_for(
                        proc.process.wait(), timeout=_KILL_REAP_TIMEOUT
                    )
                except TimeoutError:
                    logger.error(
                        "sandbox: %s not reaped %ss after SIGKILL, giving up (possible D-state)",
                        plugin_id,
                        _KILL_REAP_TIMEOUT,
                    )
        except ProcessLookupError:
            pass

        proc.health = SandboxHealth.DEAD
        logger.info("sandbox: killed %s", plugin_id)

    def health(self, plugin_id: str) -> SandboxHealth:
        # Query the sandbox health state.
        proc = self._processes.get(plugin_id)
        if proc is None:
            return SandboxHealth.DEAD
        return proc.health

    async def shutdown_all(self) -> None:
        # Shut down all sandbox subprocesses.
        for plugin_id in list(self._processes.keys()):
            await self.kill(plugin_id)

    async def _read_loop(self, proc: SandboxProcess) -> None:
        # Read messages from the subprocess stdout.
        try:
            while proc.health == SandboxHealth.ALIVE:
                line = await proc.process.stdout.readline()
                if not line:
                    proc.health = SandboxHealth.DEAD
                    logger.warning("sandbox: %s stdout EOF", proc.plugin_id)
                    self._fail_pending(
                        proc, RuntimeError(f"sandbox {proc.plugin_id!r} process exited")
                    )
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

                # P1-8: non-dict messages (e.g. a worker accidentally
                # print(5)-ing) do not kill the whole sandbox, just skip
                if not isinstance(msg, dict):
                    logger.warning(
                        "sandbox: %s dropped non-dict message: %s",
                        proc.plugin_id,
                        text[:100],
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
                elif msg_type == "rpc":
                    # R1: PROCESS sandbox resource-proxy RPC, worker calls
                    # back into the host DeskRuntime
                    await self._handle_rpc(proc, msg)
                else:
                    logger.warning(
                        "sandbox: unknown msg type %r from %s", msg_type, proc.plugin_id
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("sandbox: %s read error: %s", proc.plugin_id, e)
            proc.health = SandboxHealth.DEAD
            self._fail_pending(proc, e)

    async def _heartbeat_monitor(self, proc: SandboxProcess) -> None:
        # Monitor the subprocess heartbeat.
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
        level = str(msg.get("level", "INFO")).upper()
        message = msg.get("message", "")
        # E6: level whitelist, prevents a worker sending an invalid string
        # from resolving to a non-log method via getattr and crashing the loop
        if level not in _VALID_LOG_LEVELS:
            level = "INFO"
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("sandbox:%s: %s", proc.plugin_id, message)

    async def _drain_stderr(self, proc: SandboxProcess) -> None:
        # Continuously drain the subprocess stderr so the PIPE buffer never
        # fills up and blocks the subprocess.
        #
        # The subprocess's logs are already relayed via stdout IPC; stderr
        # only captures unexpected tracebacks.
        try:
            while proc.health == SandboxHealth.ALIVE:
                line = await proc.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.warning("sandbox:%s stderr: %s", proc.plugin_id, text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("sandbox: %s stderr drain ended: %s", proc.plugin_id, e)

    def _fail_pending(self, proc: SandboxProcess, exc: BaseException) -> None:
        # When the process exits abnormally, fail all in-flight calls with an
        # exception so callers never hang forever.
        for _call_id, future in list(proc.pending_calls.items()):
            if not future.done():
                future.set_exception(exc)
        proc.pending_calls.clear()

    def _build_worker_script(
        self, entry_point: Any, config: dict, limits: ResourceLimits
    ) -> str:
        # Build the subprocess worker script.
        #
        # Fix highlights:
        # - Entry call signature aligned with the inline contract
        #   entry(desk, params), where desk is a _DeskProxy
        # - stdout writes are locked so the heartbeat thread and the main
        #   loop no longer race and interleave
        # - Logs go through _DeskProxy.log back to the host via stdout IPC
        #   instead of stderr to avoid pipe blocking
        # - Persistent event loop + one task per call (dispatched by call_id)
        #   supports concurrent dispatch and cancellation (P1-8): the old
        #   for-line-in-stdin version was serial, a blocked call A queued
        #   call B, and stuck calls could not be cancelled.
        entry_str = ""
        if callable(entry_point):
            mod = getattr(entry_point, "__module__", "")
            qualname = getattr(entry_point, "__qualname__", "")
            if mod:
                entry_str = f"{mod}:{qualname}"
        elif isinstance(entry_point, str):
            entry_str = entry_point

        return (
            "import sys,json,time,importlib,resource,threading,asyncio,inspect\n"
            f"_ENTRY={entry_str!r}\n"
            # P0-3: config is serialized to a Python literal via repr instead
            # of json.dumps. JSON's true/false/null are not valid Python
            # identifiers; embedding them in the worker script raises
            # NameError: name 'true' is not defined, crashing the spawn of
            # every PROCESS plugin whose config carries bool/None. repr()
            # emits True/False/None, which is valid Python.
            f"_CONFIG={config!r}\n"
            f"_MEM_LIMIT_MB={limits.memory_limit_mb}\n"
            # E2: CPU limit (seconds, rounded up), 0 = unlimited.
            # RLIMIT_CPU triggers SIGXCPU
            f"_CPU_LIMIT_SEC={int(limits.cpu_limit) if limits.cpu_limit > 0 else 0}\n"
            "_OUT_LOCK=threading.Lock()\n"
            "def _send(msg):\n"
            "    line=json.dumps(msg)+'\\n'\n"
            "    with _OUT_LOCK:\n"
            "        sys.stdout.write(line)\n"
            "        sys.stdout.flush()\n"
            "def _log(level,message,**extra):\n"
            "    _send({'type':'log','level':level,'message':message,'extra':extra})\n"
            # R1: resource-proxy RPC state. _rpc_futures[rid]=Future; rpc
            # replies wake the future by id. Uses concurrent.futures.Future
            # (thread-safe); the _stdin_reader thread parses replies and
            # calls set_result.
            "import concurrent.futures as _cf\n"
            "_rpc_futures={}\n"
            "_rpc_counter=[0]\n"
            "_rpc_lock=threading.Lock()\n"
            "_RPC_TIMEOUT=60\n"
            "def _rpc_call(method,args):\n"
            "    with _rpc_lock:\n"
            "        _rpc_counter[0]+=1\n"
            "        rid=str(_rpc_counter[0])\n"
            "    fut=_cf.Future()\n"
            "    _rpc_futures[rid]=fut\n"
            "    _send({'type':'rpc','id':rid,'method':method,'args':args})\n"
            "    try:\n"
            "        return fut.result(timeout=_RPC_TIMEOUT)\n"
            "    except _cf.TimeoutError:\n"
            "        _rpc_futures.pop(rid,None)\n"
            "        raise TimeoutError('resource proxy RPC timeout: %s'%method)\n"
            "class _DeskProxy:\n"
            "    def log(self,plugin_id,level,message,**kw):\n"
            "        _log(level,message,**kw)\n"
            "    def acquire_vram(self,plugin_id,mb):\n"
            "        return _rpc_call('acquire_vram',{'plugin_id':plugin_id,'mb':mb})\n"
            "    def release_vram(self,plugin_id):\n"
            "        return _rpc_call('release_vram',{'plugin_id':plugin_id})\n"
            "    def mlx_chat(self,model,messages,**kw):\n"
            "        return _rpc_call('mlx_chat',{'model':model,'messages':messages,'kwargs':kw})\n"
            "    def get_api_key(self,provider):\n"
            "        return _rpc_call('get_api_key',{'provider':provider})\n"
            "    def check_file_permission(self,plugin_id,path):\n"
            "        return _rpc_call('check_file_permission',{'plugin_id':plugin_id,'path':path})\n"
            "desk=_DeskProxy()\n"
            "try:\n"
            "    soft,hard=resource.getrlimit(resource.RLIMIT_AS)\n"
            "    resource.setrlimit(resource.RLIMIT_AS,(_MEM_LIMIT_MB*1024*1024,hard))\n"
            "except Exception as e:\n"
            "    _log('WARNING','setrlimit RLIMIT_AS failed: %s'%e)\n"
            # E2: CPU limit (seconds). Exceeding it triggers SIGXCPU; the OS
            # terminates the worker, the host-side _read_loop sees EOF and
            # takes the CRASHED path, preventing a plugin from monopolizing
            # the CPU and wedging the host.
            "if _CPU_LIMIT_SEC>0:\n"
            "    try:\n"
            "        soft,hard=resource.getrlimit(resource.RLIMIT_CPU)\n"
            "        resource.setrlimit(resource.RLIMIT_CPU,(_CPU_LIMIT_SEC,hard))\n"
            "    except Exception as e:\n"
            "        _log('WARNING','setrlimit RLIMIT_CPU failed: %s'%e)\n"
            "def _load_entry():\n"
            "    if not _ENTRY: return None\n"
            "    if ':' in _ENTRY:\n"
            "        mod_path,_,attr=_ENTRY.partition(':')\n"
            "        mod=importlib.import_module(mod_path)\n"
            "        return getattr(mod,attr) if attr else mod\n"
            "    return importlib.import_module(_ENTRY)\n"
            "_entry_obj=_load_entry()\n"
            "if _entry_obj is None:\n"
            "    _log('ERROR','plugin entry point not configured, cannot execute')\n"
            "def _heartbeat_loop():\n"
            "    def _tick():\n"
            "        while True:\n"
            "            time.sleep(30)\n"
            "            _send({'type':'heartbeat','timestamp':time.time()})\n"
            "    t=threading.Thread(target=_tick,daemon=True)\n"
            "    t.start()\n"
            "_heartbeat_loop()\n"
            # Persistent event loop: one independent task per call, concurrent
            # dispatch; results indexed by call_id.
            "async def _execute(call_id,args):\n"
            "    if not _entry_obj:\n"
            "        _send({'type':'error','id':call_id,'error':'plugin entry point not configured'})\n"
            "        return\n"
            "    try:\n"
            "        if inspect.iscoroutinefunction(_entry_obj):\n"
            "            result=await _entry_obj(desk,args)\n"
            "        elif callable(_entry_obj):\n"
            # Sync entry points go to the thread pool so the event loop is
            # not blocked (same idea as the host's _invoke_inline to_thread)
            "            result=await asyncio.to_thread(_entry_obj,desk,args)\n"
            "        else:\n"
            "            result=str(_entry_obj)\n"
            "        _send({'type':'result','id':call_id,'result':result})\n"
            # BaseException covers SystemExit (plugin calling sys.exit),
            # converted to an error reply instead of a silently dead task
            "    except BaseException as e:\n"
            "        _send({'type':'error','id':call_id,'error':str(e)})\n"
            "async def _main():\n"
            # loop=asyncio.get_event_loop() ensures add_reader works afterwards
            "    loop=asyncio.get_event_loop()\n"
            "    loop.run_in_executor(None,_stdin_reader,loop)\n"
            "    while True:\n"
            "        await asyncio.sleep(3600)\n"
            "def _stdin_reader(loop):\n"
            "    for line in sys.stdin:\n"
            "        line=line.strip()\n"
            "        if not line: continue\n"
            "        try:\n"
            "            req=json.loads(line)\n"
            "        except json.JSONDecodeError:\n"
            "            continue\n"
            "        if req.get('type')=='rpc_result':\n"
            "            rid=req.get('id','')\n"
            "            fut=_rpc_futures.pop(rid,None)\n"
            "            if fut is None: continue\n"
            "            if req.get('error') is not None:\n"
            "                fut.set_exception(RuntimeError(req.get('error')))\n"
            "            else:\n"
            "                fut.set_result(req.get('result'))\n"
            "            continue\n"
            "        if req.get('type')!='call': continue\n"
            "        call_id=req.get('id','')\n"
            "        method=req.get('method','')\n"
            "        args=req.get('args',{})\n"
            "        if method=='execute':\n"
            "            asyncio.run_coroutine_threadsafe(_execute(call_id,args),loop)\n"
            "        else:\n"
            "            _send({'type':'error','id':call_id,'error':f'Unknown method {method}'})\n"
            "    _log('ERROR','worker stdin EOF, exiting')\n"
            "    loop.call_soon_threadsafe(loop.stop)\n"
            "asyncio.run(_main())\n"
        )
