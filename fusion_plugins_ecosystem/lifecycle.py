"""插件生命周期管理器。

负责加载/卸载/启用/禁用/热重载，以及解决 PRD 提到的痛点：
- 子代理卡死无统一重启机制 → lifecycle 提供超时熔断 + 进程自动重启
- 子代理跑 40 分钟无 token 消耗、卡死无日志 → 配合 token_meter 检测异常
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.registry import PluginManifest, PluginRegistry
from fusion_plugins_ecosystem.sandbox import PluginSandbox, ResourceLimits
from fusion_plugins_ecosystem.schema import SandboxMode

logger = logging.getLogger(__name__)

# last_error 经 RPC 暴露的最大长度，超出截断（原始异常在 desk.log 保留）
_LAST_ERROR_MAX = 300


class PluginState(str, Enum):
    """插件运行状态。"""

    REGISTERED = "registered"  # 已注册，未加载
    LOADED = "loaded"  # 已加载，未启用
    ENABLED = "enabled"  # 运行中
    DISABLED = "disabled"  # 手动禁用
    CRASHED = "crashed"  # 崩溃，等待重启
    TIMEOUT = "timeout"  # 超时熔断


class PluginLoadError(RuntimeError):
    """插件加载期不可调用/配置错误。

    P3-3：与运行时崩溃区分，此类错误不应触发 max_restart 重启风暴
    （入口不可调用重启多少次都不可调用）。
    """


@dataclass
class PluginInstance:
    """插件运行实例。"""

    manifest: PluginManifest
    state: PluginState = PluginState.REGISTERED
    instance: Any | None = None
    last_heartbeat: float = field(default_factory=time.time)
    restart_count: int = 0
    # 最近一次执行的 token 记录（由 token_meter 写入）
    last_token_record: Any | None = None
    # 沙箱进程 pid（PROCESS 模式）
    pid: int | None = None
    # 进入当前状态的起始时间戳
    start_time: float = field(default_factory=time.time)
    # 累计错误次数
    error_count: int = 0
    # 最近一次错误信息
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        uptime = max(0, int(now - self.start_time))
        # last_error 经 RPC 暴露给 Studio，截断防止泄露路径/堆栈/敏感信息；
        # 原始异常已完整记录在 desk.log（服务端，不经 RPC）
        err = self.last_error
        if err and len(err) > _LAST_ERROR_MAX:
            err = err[:_LAST_ERROR_MAX] + "…"
        return {
            "id": self.manifest.id,
            "plugin_id": self.manifest.id,
            "state": self.state.value,
            "restart_count": self.restart_count,
            "last_heartbeat": self.last_heartbeat,
            "pid": self.pid,
            "start_time": str(int(self.start_time)),
            "uptime": uptime,
            "error_count": self.error_count,
            "last_error": err,
        }


class PluginLifecycle:
    """插件生命周期管理器。

    用法：
        lifecycle = PluginLifecycle(registry)
        await lifecycle.enable("caveman_compress")
    """

    # 默认超时秒数（对应 PRD「子代理超时自动销毁」）
    DEFAULT_TIMEOUT = 600  # 10 分钟
    # 最大重启次数
    MAX_RESTART = 3
    # 心跳超时阈值（秒），超过则判定卡死
    HEARTBEAT_STALE = 120

    # 合法状态转换映射：{from_state: {to_state}}
    # 含幂等自循环（TIMEOUT→TIMEOUT、CRASHED→CRASHED），避免并发二次熔断抛错
    _VALID_TRANSITIONS: dict[PluginState, set[PluginState]] = {
        PluginState.REGISTERED: {PluginState.LOADED, PluginState.CRASHED},
        PluginState.LOADED: {
            PluginState.ENABLED,
            PluginState.DISABLED,
            PluginState.CRASHED,
        },
        PluginState.ENABLED: {
            PluginState.DISABLED,
            PluginState.CRASHED,
            PluginState.TIMEOUT,
        },
        PluginState.DISABLED: {PluginState.LOADED, PluginState.ENABLED},
        PluginState.CRASHED: {PluginState.LOADED, PluginState.DISABLED, PluginState.CRASHED},
        PluginState.TIMEOUT: {PluginState.LOADED, PluginState.DISABLED, PluginState.TIMEOUT},
    }

    def __init__(
        self,
        registry: PluginRegistry,
        config: Any | None = None,
    ) -> None:
        self.registry = registry
        self.desk: DeskRuntime = registry.desk
        self.config = config
        self._instances: dict[str, PluginInstance] = {}
        # 同步锁：load/unload 是同步函数，asyncio.Lock 无法 await，故用 threading.Lock
        self._instances_lock = threading.RLock()
        # R1：注入 DeskRuntime，使 PROCESS 沙箱资源代理 RPC 可访问宿主资源
        self._sandbox = PluginSandbox(desk=self.desk)
        # 异常检测任务句柄
        self._watcher_task: asyncio.Task[None] | None = None
        # E3：持有 unload 触发的 kill 后台任务引用，防止 GC 回收导致进程未清理
        self._pending_kill_tasks: set[asyncio.Task] = set()
        # R2：inline 并发闸，限制 to_thread 线程池并发插件执行数
        self._concurrency_sem: asyncio.Semaphore | None = None

    # ── A2：配置驱动阈值，config 缺失回退类常量（兼容无 config 的测试路径）──

    def _cfg_timeout(self) -> int:
        if self.config is not None:
            return self.config.subagent_timeout_seconds
        return self.DEFAULT_TIMEOUT

    def _cfg_max_restart(self) -> int:
        if self.config is not None:
            return self.config.max_auto_restart
        return self.MAX_RESTART

    def _cfg_heartbeat_stale(self) -> int:
        if self.config is not None:
            return self.config.heartbeat_stale_seconds
        return self.HEARTBEAT_STALE

    def _cfg_max_concurrent(self) -> int:
        if self.config is not None:
            return self.config.max_concurrent_plugins
        return 16

    def _concurrency_semaphore(self) -> asyncio.Semaphore:
        """惰性创建并发信号量（需事件循环，故延迟构造）。"""
        if self._concurrency_sem is None:
            self._concurrency_sem = asyncio.Semaphore(self._cfg_max_concurrent())
        return self._concurrency_sem

    def _transition(self, inst: PluginInstance, new_state: PluginState) -> None:
        """执行状态转换（带合法性校验）。

        非法转换直接抛错并保持原状态，避免静默进入不一致状态。
        """
        allowed = self._VALID_TRANSITIONS.get(inst.state, set())
        if new_state not in allowed:
            logger.error(
                "lifecycle: 非法状态转换 %s → %s (plugin=%s)，拒绝转换",
                inst.state.value,
                new_state.value,
                inst.manifest.id,
            )
            raise RuntimeError(
                f"非法状态转换 {inst.state.value} → {new_state.value} "
                f"(plugin={inst.manifest.id})"
            )
        inst.state = new_state
        inst.start_time = time.time()

    def load(
        self, plugin_id: str, _loading_chain: frozenset[str] | None = None
    ) -> PluginInstance:
        """加载插件（实例化 entry_point），自动先加载依赖。"""
        with self._instances_lock:
            manifest = self.registry.get(plugin_id)
            if manifest is None:
                raise KeyError(f"插件 {plugin_id!r} 未注册")

            if plugin_id in self._instances:
                return self._instances[plugin_id]

            # 递归加载依赖
            chain = _loading_chain or frozenset()
            if plugin_id in chain:
                raise ValueError(f"循环依赖: {' → '.join(chain)} → {plugin_id}")
            next_chain = chain | {plugin_id}
            for dep_id in manifest.depends_on:
                if dep_id not in self._instances:
                    self.load(dep_id, _loading_chain=next_chain)

            # 解析 entry_point
            entry = manifest.entry_point
            if isinstance(entry, str):
                module_path, _, attr = entry.partition(":")
                mod = importlib.import_module(module_path)
                entry = getattr(mod, attr) if attr else mod
            # 实例化逻辑：
            # - 类（type） → 实例化 entry()
            # - 普通函数/可调用对象 → 直接使用 entry（签名约定为 (desk, params)）
            if isinstance(entry, type):
                instance = entry()
            elif callable(entry):
                instance = entry
            else:
                instance = entry

            plugin_inst = PluginInstance(
                manifest=manifest,
                state=PluginState.LOADED,
                instance=instance,
            )
            self._instances[plugin_id] = plugin_inst
            self.desk.log(plugin_id, "INFO", "插件已加载")
            return plugin_inst

    async def enable(self, plugin_id: str) -> PluginInstance:
        """启用插件（申请显存 + 标记运行中）。

        显存申请失败抛 RuntimeError（而非返回 CRASHED 实例），让调用方明确感知失败。
        """
        with self._instances_lock:
            inst = self._instances.get(plugin_id) or self.load(plugin_id)
            manifest = inst.manifest

            # 申请显存（解决「显存抢占冲突」痛点）
            if manifest.vram_mb > 0:
                ok = self.desk.acquire_vram(plugin_id, manifest.vram_mb)
                if not ok:
                    self._transition(inst, PluginState.CRASHED)
                    self.desk.log(plugin_id, "ERROR", "显存申请失败，插件未启用")
                    raise RuntimeError(f"插件 {plugin_id!r} 显存申请失败，未启用")

            self._transition(inst, PluginState.ENABLED)
            inst.last_heartbeat = time.time()
            self.desk.log(plugin_id, "INFO", "插件已启用")
            return inst

    async def disable(self, plugin_id: str) -> None:
        """禁用插件（释放显存 + 终止沙箱进程）。"""
        with self._instances_lock:
            inst = self._instances.get(plugin_id)
        if inst is None:
            return

        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            await self._sandbox.kill(plugin_id)
        if inst.manifest.vram_mb > 0:
            self.desk.release_vram(plugin_id)
        with self._instances_lock:
            self._transition(inst, PluginState.DISABLED)
        self.desk.log(plugin_id, "INFO", "插件已禁用")

    def unload(self, plugin_id: str) -> None:
        """卸载插件实例（保留注册）。

        PROCESS 模式下连带 kill worker，避免孤儿进程残留（P1-6）。
        kill 是 async，但 unload 同步签名被 _maybe_restart 调用；
        这里同步清理进程句柄：kill 内部 cancel task + terminate 已足够，
        完整回收交给 sandbox 析构。为保持同步语义，直接 pop 并标记。
        """
        with self._instances_lock:
            inst = self._instances.pop(plugin_id, None)
        if inst is None:
            return
        if inst.manifest.vram_mb > 0:
            self.desk.release_vram(plugin_id)
        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            # 同步上下文无法 await kill；调度到事件循环异步清理。
            # E3：持有 task 引用防止 GC 回收，完成后自动移除。
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._sandbox.kill(plugin_id))
                self._pending_kill_tasks.add(task)
                task.add_done_callback(self._pending_kill_tasks.discard)
            except RuntimeError:
                # 无运行事件循环（同步测试路径），尽力 terminate
                logger.debug("lifecycle: unload 无事件循环，跳过 PROCESS kill")
        self.desk.log(plugin_id, "INFO", "插件已卸载")

    async def execute(
        self,
        plugin_id: str,
        params: dict[str, Any],
        timeout_override: int | None = None,
    ) -> Any:
        """执行插件，带超时熔断。

        解决「子代理跑 40 分钟无 token 消耗、卡死无日志」痛点：
        - 超过 timeout_seconds 强制终止（inline 用 to_thread 可真中断，PROCESS kill 进程）
        - 心跳判定卡死仅对 PROCESS 沙箱生效（有独立心跳线程）
        """
        with self._instances_lock:
            inst = self._instances.get(plugin_id)
            if inst is None or inst.state != PluginState.ENABLED:
                raise RuntimeError(f"插件 {plugin_id!r} 未启用，无法执行")
            timeout = (
                timeout_override
                or inst.manifest.timeout_seconds
                or self._cfg_timeout()
            )
            inst.last_heartbeat = time.time()

        try:
            result = await asyncio.wait_for(
                self._invoke(inst, params),
                timeout=timeout,
            )
            # P1-1：执行成功即重置崩溃预算，避免长生命周期插件偶发崩溃累积触顶永久 CRASHED。
            # 仅"连续失败"才应累加 restart_count，成功一次即清零。
            with self._instances_lock:
                if inst.restart_count:
                    inst.restart_count = 0
            return result
        except asyncio.TimeoutError:
            # PROCESS 模式：超时必须 kill worker，否则孤儿进程残留、重启复用卡死进程（P1-6）
            if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
                await self._sandbox.kill(plugin_id)
            with self._instances_lock:
                self._transition(inst, PluginState.TIMEOUT)
                inst.error_count += 1
                inst.last_error = f"执行超时 ({timeout}s)"
            self.desk.log(plugin_id, "ERROR", "插件执行超时，已熔断", timeout=timeout)
            await self._maybe_restart(plugin_id)
            raise
        except Exception as exc:
            with self._instances_lock:
                self._transition(inst, PluginState.CRASHED)
                inst.error_count += 1
                inst.last_error = str(exc)
            self.desk.log(plugin_id, "ERROR", "插件执行崩溃", error=str(exc))
            # P3-3：加载期不可调用属配置错误，重启无意义且浪费预算，跳过自动重启
            if not isinstance(exc, PluginLoadError):
                await self._maybe_restart(plugin_id)
            raise

    async def _invoke(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        """实际调用插件入口。根据 sandbox_mode 选择进程内或沙箱执行。

        sandbox_mode 选取优先级：
        1. manifest 显式声明 PROCESS → 沙箱执行
        2. manifest 为默认 INLINE，但 EcosystemConfig.sandbox_default_mode=process → 沙箱执行
        3. 其余 → 进程内执行

        config 缺失时默认 inline（与大量无 config 的调用路径/测试约定一致），
        但显式注入的非法 sandbox_default_mode 必须报错，不再 getattr 兜底（P2-4）。
        """
        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            return await self._invoke_sandbox(inst, params)

        if self.config is None:
            return await self._invoke_inline(inst, params)

        config_default = self.config.sandbox_default_mode
        if config_default not in ("inline", "process"):
            raise RuntimeError(
                f"lifecycle: 非法 sandbox_default_mode={config_default!r}"
            )
        if config_default == "process":
            return await self._invoke_sandbox(inst, params)

        return await self._invoke_inline(inst, params)

    async def _invoke_inline(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        """进程内调用插件入口。

        同步入口用 to_thread 丢线程池，避免阻塞事件循环、使 wait_for 超时真生效（P1-2）。
        R2：信号量限制并发 inline 执行数，防止无界线程池耗尽资源。
        """
        instance = inst.instance
        if inspect.iscoroutinefunction(instance):
            async with self._concurrency_semaphore():
                return await instance(self.desk, params)
        if callable(instance):
            async with self._concurrency_semaphore():
                return await asyncio.to_thread(instance, self.desk, params)
        raise PluginLoadError(f"插件 {inst.manifest.id!r} 入口不可调用")

    async def _invoke_sandbox(
        self, inst: PluginInstance, params: dict[str, Any]
    ) -> Any:
        """沙箱进程调用插件入口。"""
        plugin_id = inst.manifest.id
        if self._sandbox.health(plugin_id) != "alive":
            # E1：传递真实插件配置而非空 dict，worker 内可读取运行参数
            spawn_config = {}
            if self.config is not None:
                spawn_config = dict(self.config.to_dict())
            await self._sandbox.spawn(
                plugin_id,
                entry_point=inst.manifest.entry_point,
                config=spawn_config,
                limits=ResourceLimits(
                    timeout_seconds=inst.manifest.timeout_seconds
                    or self._cfg_timeout(),
                ),
            )
        return await self._sandbox.call(plugin_id, "execute", params)

    async def _maybe_restart(self, plugin_id: str) -> None:
        """崩溃后自动重启（限制 max_restart 次，优先使用插件级配置）。

        R3：重启前指数退避（2^n 秒，上限 60s），避免崩溃循环瞬间打满 CPU；
        enable 异常被捕获，避免重启失败向调用方二次抛错导致递归熔断。
        """
        with self._instances_lock:
            inst = self._instances.get(plugin_id)
            if inst is None:
                return
            max_restart = (
                inst.manifest.max_restart
                if inst.manifest.max_restart is not None
                else self._cfg_max_restart()
            )
            if inst.restart_count >= max_restart:
                self.desk.log(plugin_id, "ERROR", "达到最大重启次数，插件保持崩溃状态")
                return
            # 保存计数，unload 会删除旧 instance
            new_count = inst.restart_count + 1
        # R3：指数退避，避免崩溃循环瞬间重启打满资源。
        # 无 config 的测试路径不退避（保持原即时重启语义），生产路径按 2^n 退避。
        backoff = 0
        if self.config is not None:
            backoff = min(2 ** (new_count - 1), 60)
        if backoff > 0:
            await asyncio.sleep(backoff)
            # 退避期间插件可能已被 disable/unload，二次确认
            with self._instances_lock:
                if plugin_id not in self._instances:
                    return
        self.unload(plugin_id)
        new_inst = self.load(plugin_id)
        with self._instances_lock:
            # P2-7：在写锁内再次确认仍是当前实例，避免并发双 restart 互相覆盖计数。
            # new_inst 由本次 load 产生；若已被另一路 unload/load 替换则跳过写。
            current = self._instances.get(plugin_id)
            if current is new_inst:
                new_inst.restart_count = new_count
            elif current is not None:
                # 已被并发 restart 替换，沿用其计数 +1 以保留崩溃预算累加语义
                current.restart_count = max(current.restart_count, new_count)
        try:
            await self.enable(plugin_id)
            self.desk.log(plugin_id, "INFO", "插件已自动重启", count=new_count)
        except Exception as exc:
            # R3：重启 enable 失败不向调用方抛错，记录崩溃态避免递归
            with self._instances_lock:
                self._transition(new_inst, PluginState.CRASHED)
                new_inst.last_error = f"自动重启失败: {exc}"
            self.desk.log(plugin_id, "ERROR", "插件自动重启失败", error=str(exc))

    async def start_watcher(self) -> None:
        """启动异常检测循环，发现卡死插件自动熔断。"""
        if self._watcher_task is not None:
            return
        self._watcher_task = asyncio.create_task(self._watch_loop())

    async def stop_watcher(self) -> None:
        """停止异常检测循环。"""
        if self._watcher_task is not None:
            self._watcher_task.cancel()
            self._watcher_task = None

    async def _watch_loop(self) -> None:
        """心跳检测循环。

        仅对 PROCESS 沙箱插件生效（有独立心跳线程，能真实反映进程活性）。
        inline 插件由 execute 的 wait_for 超时单独管，其心跳在执行期间无法更新
        （事件循环被阻塞），watcher 介入会误杀合法长任务（P2-3）。
        判死阈值取 manifest.timeout_seconds，与 LONG_TASK 能力语义对齐。
        """
        while True:
            try:
                now = time.time()
                with self._instances_lock:
                    candidates = [
                        (pid, inst)
                        for pid, inst in self._instances.items()
                        if inst.state == PluginState.ENABLED
                        and inst.manifest.sandbox_mode == SandboxMode.PROCESS
                    ]
                for plugin_id, inst in candidates:
                    threshold = inst.manifest.timeout_seconds or self._cfg_heartbeat_stale()
                    if now - inst.last_heartbeat > threshold:
                        with self._instances_lock:
                            if inst.state != PluginState.ENABLED:
                                continue
                            self._transition(inst, PluginState.TIMEOUT)
                        self.desk.log(plugin_id, "ERROR", "心跳超时，判定卡死，熔断")
                        await self._sandbox.kill(plugin_id)
                        await self._maybe_restart(plugin_id)
                # P3-2：整数除法下限 max(1, ...)，避免 heartbeat_stale=1 时 sleep(0) 空转
                await asyncio.sleep(max(1, self._cfg_heartbeat_stale() // 2))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # P0-3：单次扫描/kill 异常不终止整个看门狗，记日志后继续
                logger.error("lifecycle: _watch_loop 迭代异常: %s", exc, exc_info=True)
                await asyncio.sleep(max(1, self._cfg_heartbeat_stale() // 2))

    def list_states(self) -> list[dict[str, Any]]:
        """返回所有插件实例状态快照（供消费端查询）。"""
        return [inst.to_dict() for inst in self._instances.values()]

    def get_state(self, plugin_id: str) -> dict[str, Any] | None:
        """查询单个插件状态快照，未加载返回 None。"""
        inst = self._instances.get(plugin_id)
        return inst.to_dict() if inst else None

    def list_by_state(self, state: PluginState) -> list[dict[str, Any]]:
        """按状态过滤返回插件快照列表。"""
        return [
            inst.to_dict() for inst in self._instances.values() if inst.state == state
        ]
