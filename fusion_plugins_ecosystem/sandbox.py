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

# P0-2：SIGKILL 后回收等待上限。超出即放弃等待（进程可能处不可中断 D-state），
# 避免无超时 await 永久挂起调用链。
_KILL_REAP_TIMEOUT = 5

# P1-8/E6：日志级别白名单，防 worker 发非法 level 字符串经 getattr 解析到非日志方法。
_VALID_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


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
    stderr_task: asyncio.Task | None = None


class PluginSandbox:
    """插件沙箱管理器（宿主侧）。

    用法：
        sandbox = PluginSandbox()
        await sandbox.spawn("my_plugin", entry_point, config)
        result = await sandbox.call("my_plugin", "execute", {"input": "hello"})
        await sandbox.kill("my_plugin")
    """

    def __init__(
        self,
        default_limits: ResourceLimits | None = None,
        desk: Any = None,
    ) -> None:
        self._processes: dict[str, SandboxProcess] = {}
        self.default_limits = default_limits or ResourceLimits()
        # R1：宿主 DeskRuntime 句柄，供 worker 资源代理 RPC 回调
        self.desk = desk

    async def _handle_rpc(self, proc: SandboxProcess, msg: dict) -> None:
        """处理 worker 回调宿主的资源请求（R1：PROCESS 沙箱资源代理）。

        worker 内 _DeskProxy 的 acquire_vram/mlx_chat/check_file_permission/
        get_api_key 通过 stdout 发 rpc 消息回调宿主，宿主执行后经 stdin 回写结果。
        """
        rpc_id = msg.get("id", "")
        method = msg.get("method", "")
        args = msg.get("args", {})
        result: Any = None
        error: str | None = None
        try:
            if self.desk is None:
                raise RuntimeError("宿主 DeskRuntime 未注入，无法代理资源请求")
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
                # 异步方法，需 await
                result = await self.desk.mlx_chat(
                    args.get("model", ""),
                    args.get("messages", []),
                    **args.get("kwargs", {}),
                )
            else:
                raise RuntimeError(f"未知 rpc 方法 {method!r}")
        except Exception as exc:
            error = str(exc)
            logger.warning("sandbox: rpc %s 失败: %s", method, exc)
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
            logger.warning("sandbox: rpc 回写失败: %s", exc)

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
        sandbox_proc.stderr_task = asyncio.create_task(self._drain_stderr(sandbox_proc))

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

        try:
            return await asyncio.wait_for(future, timeout=proc.limits.timeout_seconds)
        except asyncio.TimeoutError:
            proc.pending_calls.pop(call_id, None)
            # 超时后必须 kill worker：否则孤儿进程残留，下次 call 复用卡死进程（P0-4）。
            # _invoke_sandbox 在 health!=alive 时会 respawn。
            self.desk_log(proc, "ERROR", f"call {call_id} 超时，kill worker 并等待 respawn")
            await self.kill(plugin_id)
            raise TimeoutError(
                f"sandbox: plugin {plugin_id!r} call timed out after "
                f"{proc.limits.timeout_seconds}s"
            )

    def desk_log(self, proc: SandboxProcess, level: str, message: str, **kw: Any) -> None:
        """沙箱侧日志经宿主 logger 输出（worker 自身日志已通过 IPC 回传）。"""
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("sandbox:%s: %s %s", proc.plugin_id, message, kw or "")

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
            except asyncio.TimeoutError:
                proc.process.kill()
                # P0-2：SIGKILL 后等待回收加超时，避免 worker 进入不可中断 D-state
                # 时此 await 永久挂起、连带拖垮 execute/_watch_loop。
                try:
                    await asyncio.wait_for(
                        proc.process.wait(), timeout=_KILL_REAP_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "sandbox: %s SIGKILL 后 %ss 未回收，放弃等待（可能 D-state）",
                        plugin_id,
                        _KILL_REAP_TIMEOUT,
                    )
        except ProcessLookupError:
            pass

        proc.health = SandboxHealth.DEAD
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
                    self._fail_pending(
                        proc, RuntimeError(f"sandbox {proc.plugin_id!r} 进程已退出")
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

                # P1-8：非 dict 消息（如 worker 误 print(5)）不击杀整个沙箱，跳过即可
                if not isinstance(msg, dict):
                    logger.warning(
                        "sandbox: %s 非 dict 消息已丢弃: %s",
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
                    # R1：PROCESS 沙箱资源代理 RPC，worker 回调宿主 DeskRuntime
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
        level = str(msg.get("level", "INFO")).upper()
        message = msg.get("message", "")
        # E6：level 白名单，防 worker 发非法字符串经 getattr 解析到非日志方法崩循环
        if level not in _VALID_LOG_LEVELS:
            level = "INFO"
        log_method = getattr(logger, level.lower(), logger.info)
        log_method("sandbox:%s: %s", proc.plugin_id, message)

    async def _drain_stderr(self, proc: SandboxProcess) -> None:
        """持续读取子进程 stderr，防止 PIPE 缓冲区写满导致子进程阻塞。

        子进程的日志已通过 stdout IPC 回传，stderr 只捕获意外的 traceback。
        """
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
        """进程异常退出时，让所有等待中的调用以异常结束，避免调用方永久挂起。"""
        for call_id, future in list(proc.pending_calls.items()):
            if not future.done():
                future.set_exception(exc)
        proc.pending_calls.clear()

    def _build_worker_script(
        self, entry_point: Any, config: dict, limits: ResourceLimits
    ) -> str:
        """构建子进程 worker 脚本。

        修复要点：
        - 入口调用签名对齐 inline 契约 entry(desk, params)，desk 为 _DeskProxy
        - stdout 输出加锁，心跳线程与主循环不再竞争交错
        - 日志经 _DeskProxy.log 通过 stdout IPC 回传宿主，不再写 stderr 避免管道阻塞
        - 持久事件循环 + 每次调用独立 task（按 call_id 调度），支持并发分发与取消（P1-8）：
          旧版 for-line-in-stdin 串行，调用 A 阻塞则调用 B 排队，且无法取消卡死调用。
        """
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
            f"_CONFIG={json.dumps(config)}\n"
            f"_MEM_LIMIT_MB={limits.memory_limit_mb}\n"
            # E2：CPU 限时（秒，向上取整），0 表示不限。RLIMIT_CPU 触发 SIGXCPU
            f"_CPU_LIMIT_SEC={int(limits.cpu_limit) if limits.cpu_limit > 0 else 0}\n"
            "_OUT_LOCK=threading.Lock()\n"
            "def _send(msg):\n"
            "    line=json.dumps(msg)+'\\n'\n"
            "    with _OUT_LOCK:\n"
            "        sys.stdout.write(line)\n"
            "        sys.stdout.flush()\n"
            "def _log(level,message,**extra):\n"
            "    _send({'type':'log','level':level,'message':message,'extra':extra})\n"
            # R1：资源代理 RPC 状态。_rpc_futures[rid]=Future，rpc 回包按 id 唤醒。
            # 用 concurrent.futures.Future（线程安全），_stdin_reader 线程解析回包后 set_result。
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
            "        raise TimeoutError('资源代理 RPC 超时: %s'%method)\n"
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
            # E2：CPU 限时（秒）。超限触发 SIGXCPU，worker 被 OS 终止，宿主侧
            # _read_loop 收到 EOF 走 CRASHED 路径，避免插件独占 CPU 卡死宿主。
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
            "    _log('ERROR','插件入口未配置，无法执行')\n"
            "def _heartbeat_loop():\n"
            "    def _tick():\n"
            "        while True:\n"
            "            time.sleep(30)\n"
            "            _send({'type':'heartbeat','timestamp':time.time()})\n"
            "    t=threading.Thread(target=_tick,daemon=True)\n"
            "    t.start()\n"
            "_heartbeat_loop()\n"
            # 持久事件循环：每个 call 起独立 task，并发分发；按 call_id 索引结果。
            "async def _execute(call_id,args):\n"
            "    if not _entry_obj:\n"
            "        _send({'type':'error','id':call_id,'error':'插件入口未配置'})\n"
            "        return\n"
            "    try:\n"
            "        if inspect.iscoroutinefunction(_entry_obj):\n"
            "            result=await _entry_obj(desk,args)\n"
            "        elif callable(_entry_obj):\n"
            # 同步入口丢线程池，不阻塞事件循环（与宿主 _invoke_inline to_thread 同理）
            "            result=await asyncio.to_thread(_entry_obj,desk,args)\n"
            "        else:\n"
            "            result=str(_entry_obj)\n"
            "        _send({'type':'result','id':call_id,'result':result})\n"
            # BaseException 覆盖 SystemExit（插件 sys.exit 场景），转为 error 回传而非静默死 task
            "    except BaseException as e:\n"
            "        _send({'type':'error','id':call_id,'error':str(e)})\n"
            "async def _main():\n"
            # loop=asyncio.get_event_loop() 保证后续 add_reader 可用
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
            "    _log('ERROR','worker stdin EOF，退出')\n"
            "    loop.call_soon_threadsafe(loop.stop)\n"
            "asyncio.run(_main())\n"
        )
