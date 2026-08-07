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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fusion_plugins_ecosystem.desk_runtime import DeskRuntime
from fusion_plugins_ecosystem.registry import PluginManifest, PluginRegistry
from fusion_plugins_ecosystem.sandbox import PluginSandbox, ResourceLimits

logger = logging.getLogger(__name__)


class PluginState(str, Enum):
    """插件运行状态。"""

    REGISTERED = "registered"  # 已注册，未加载
    LOADED = "loaded"  # 已加载，未启用
    ENABLED = "enabled"  # 运行中
    DISABLED = "disabled"  # 手动禁用
    CRASHED = "crashed"  # 崩溃，等待重启
    TIMEOUT = "timeout"  # 超时熔断


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.manifest.id,
            "state": self.state.value,
            "restart_count": self.restart_count,
            "last_heartbeat": self.last_heartbeat,
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
        PluginState.CRASHED: {PluginState.LOADED, PluginState.DISABLED},
        PluginState.TIMEOUT: {PluginState.LOADED, PluginState.DISABLED},
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
        self._instances_lock = asyncio.Lock()
        self._sandbox = PluginSandbox()
        # 异常检测任务句柄
        self._watcher_task: asyncio.Task[None] | None = None

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

    def load(
        self, plugin_id: str, _loading_chain: frozenset[str] | None = None
    ) -> PluginInstance:
        """加载插件（实例化 entry_point），自动先加载依赖。"""
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
        """启用插件（申请显存 + 标记运行中）。"""
        inst = self._instances.get(plugin_id) or self.load(plugin_id)
        manifest = inst.manifest

        # 申请显存（解决「显存抢占冲突」痛点）
        if manifest.vram_mb > 0:
            ok = self.desk.acquire_vram(plugin_id, manifest.vram_mb)
            if not ok:
                self._transition(inst, PluginState.CRASHED)
                self.desk.log(plugin_id, "ERROR", "显存申请失败，插件未启用")
                return inst

        self._transition(inst, PluginState.ENABLED)
        inst.last_heartbeat = time.time()
        self.desk.log(plugin_id, "INFO", "插件已启用")
        return inst

    async def disable(self, plugin_id: str) -> None:
        """禁用插件（释放显存 + 终止沙箱进程）。"""
        inst = self._instances.get(plugin_id)
        if inst is None:
            return
        from fusion_plugins_ecosystem.schema import SandboxMode

        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            await self._sandbox.kill(plugin_id)
        if inst.manifest.vram_mb > 0:
            self.desk.release_vram(plugin_id)
        self._transition(inst, PluginState.DISABLED)
        self.desk.log(plugin_id, "INFO", "插件已禁用")

    def unload(self, plugin_id: str) -> None:
        """卸载插件实例（保留注册）。"""
        if plugin_id in self._instances:
            inst = self._instances[plugin_id]
            if inst.manifest.vram_mb > 0:
                self.desk.release_vram(plugin_id)
            del self._instances[plugin_id]
            self.desk.log(plugin_id, "INFO", "插件已卸载")

    async def execute(
        self,
        plugin_id: str,
        params: dict[str, Any],
        timeout_override: int | None = None,
    ) -> Any:
        """执行插件，带超时熔断。

        解决「子代理跑 40 分钟无 token 消耗、卡死无日志」痛点：
        - 超过 timeout_seconds 强制终止
        - 心跳停止 HEARTBEAT_STALE 秒判定卡死
        """
        inst = self._instances.get(plugin_id)
        if inst is None or inst.state != PluginState.ENABLED:
            raise RuntimeError(f"插件 {plugin_id!r} 未启用，无法执行")

        timeout = (
            timeout_override or inst.manifest.timeout_seconds or self.DEFAULT_TIMEOUT
        )
        inst.last_heartbeat = time.time()

        try:
            result = await asyncio.wait_for(
                self._invoke(inst, params),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            self._transition(inst, PluginState.TIMEOUT)
            self.desk.log(plugin_id, "ERROR", "插件执行超时，已熔断", timeout=timeout)
            await self._maybe_restart(plugin_id)
            raise
        except Exception as exc:
            self._transition(inst, PluginState.CRASHED)
            self.desk.log(plugin_id, "ERROR", "插件执行崩溃", error=str(exc))
            await self._maybe_restart(plugin_id)
            raise

    async def _invoke(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        """实际调用插件入口。根据 sandbox_mode 选择进程内或沙箱执行。

        sandbox_mode 选取优先级：
        1. manifest 显式声明 PROCESS → 沙箱执行
        2. manifest 为默认 INLINE，但 EcosystemConfig.sandbox_default_mode=process → 沙箱执行
        3. 其余 → 进程内执行
        """
        from fusion_plugins_ecosystem.schema import SandboxMode

        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            return await self._invoke_sandbox(inst, params)

        config_default = getattr(self.config, "sandbox_default_mode", "inline")
        if config_default == "process":
            return await self._invoke_sandbox(inst, params)

        return await self._invoke_inline(inst, params)

    async def _invoke_inline(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        """进程内调用插件入口。"""
        instance = inst.instance
        if inspect.iscoroutinefunction(instance):
            return await instance(self.desk, params)
        if callable(instance):
            return instance(self.desk, params)
        raise RuntimeError(f"插件 {inst.manifest.id!r} 入口不可调用")

    async def _invoke_sandbox(
        self, inst: PluginInstance, params: dict[str, Any]
    ) -> Any:
        """沙箱进程调用插件入口。"""
        plugin_id = inst.manifest.id
        if self._sandbox.health(plugin_id) != "alive":
            await self._sandbox.spawn(
                plugin_id,
                entry_point=inst.manifest.entry_point,
                config={},
                limits=ResourceLimits(
                    timeout_seconds=inst.manifest.timeout_seconds
                    or self.DEFAULT_TIMEOUT,
                ),
            )
        return await self._sandbox.call(plugin_id, "execute", params)

    async def _maybe_restart(self, plugin_id: str) -> None:
        """崩溃后自动重启（限制 max_restart 次，优先使用插件级配置）。"""
        inst = self._instances.get(plugin_id)
        if inst is None:
            return
        max_restart = (
            inst.manifest.max_restart
            if inst.manifest.max_restart is not None
            else self.MAX_RESTART
        )
        if inst.restart_count >= max_restart:
            self.desk.log(plugin_id, "ERROR", "达到最大重启次数，插件保持崩溃状态")
            return
        # 保存计数，unload 会删除旧 instance
        new_count = inst.restart_count + 1
        self.unload(plugin_id)
        new_inst = self.load(plugin_id)
        new_inst.restart_count = new_count
        await self.enable(plugin_id)
        self.desk.log(plugin_id, "INFO", "插件已自动重启", count=new_count)

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
        """心跳检测循环：HEARTBEAT_STALE 秒无心跳判定卡死。"""
        while True:
            now = time.time()
            for plugin_id, inst in list(self._instances.items()):
                if inst.state != PluginState.ENABLED:
                    continue
                if now - inst.last_heartbeat > self.HEARTBEAT_STALE:
                    self._transition(inst, PluginState.TIMEOUT)
                    self.desk.log(plugin_id, "ERROR", "心跳超时，判定卡死，熔断")
                    await self._maybe_restart(plugin_id)
            await asyncio.sleep(self.HEARTBEAT_STALE // 2)

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
