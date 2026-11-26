# Plugin lifecycle manager.
# Handles load/unload/enable/disable/hot-reload and addresses the PRD pain
# points:
# - Subagents stuck with no unified restart mechanism → lifecycle provides
#   timeout meltdown + automatic process restart
# - Subagents running 40 minutes with zero token consumption and no logs →
#   anomaly detection in coordination with token_meter

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

# Max length of last_error exposed over RPC; truncated beyond this (the
# original exception is preserved in desk.log)
_LAST_ERROR_MAX = 300


# Plugin runtime state.
class PluginState(str, Enum):
    REGISTERED = "registered"  # registered, not loaded
    LOADED = "loaded"  # loaded, not enabled
    ENABLED = "enabled"  # running
    DISABLED = "disabled"  # manually disabled
    CRASHED = "crashed"  # crashed, awaiting restart
    TIMEOUT = "timeout"  # timeout meltdown


# Plugin not-callable/config error at load time.
# P3-3: distinct from runtime crashes — this class of error must not trigger a
# max_restart restart storm (an entry point that is not callable will never
# become callable by restarting).
class PluginLoadError(RuntimeError):
    pass


@dataclass
# Plugin runtime instance.
class PluginInstance:
    manifest: PluginManifest
    state: PluginState = PluginState.REGISTERED
    instance: Any | None = None
    last_heartbeat: float = field(default_factory=time.time)
    restart_count: int = 0
    # Token record of the most recent execution (written by token_meter)
    last_token_record: Any | None = None
    # Sandbox process pid (PROCESS mode)
    pid: int | None = None
    # Timestamp of entry into the current state
    start_time: float = field(default_factory=time.time)
    # Cumulative error count
    error_count: int = 0
    # Most recent error message
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        uptime = max(0, int(now - self.start_time))
        # last_error is exposed to Studio over RPC; truncate to avoid leaking
        # paths/stacks/sensitive info. The original exception is logged in
        # full to desk.log (server side, not over RPC).
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
    # Plugin lifecycle manager.
    # Usage:
    #     lifecycle = PluginLifecycle(registry)
    #     await lifecycle.enable("caveman_compress")
    # Default timeout seconds (corresponds to the PRD "auto-destroy subagent
    # on timeout")
    DEFAULT_TIMEOUT = 600  # 10 minutes
    # Max restart count
    MAX_RESTART = 3
    # Heartbeat stale threshold (seconds); beyond this a plugin is judged stuck
    HEARTBEAT_STALE = 120

    # Valid state transition map: {from_state: {to_state}}
    # Includes idempotent self-loops (TIMEOUT→TIMEOUT, CRASHED→CRASHED) so a
    # concurrent second meltdown does not raise.
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
        PluginState.CRASHED: {
            PluginState.LOADED,
            PluginState.DISABLED,
            PluginState.CRASHED,
        },
        PluginState.TIMEOUT: {
            PluginState.LOADED,
            PluginState.DISABLED,
            PluginState.TIMEOUT,
        },
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
        # Sync lock: load/unload are sync functions where asyncio.Lock cannot
        # be awaited, hence threading.Lock
        self._instances_lock = threading.RLock()
        # R1: inject DeskRuntime so the PROCESS sandbox resource proxy RPC can
        # reach host resources
        self._sandbox = PluginSandbox(desk=self.desk)
        # Anomaly detection task handle
        self._watcher_task: asyncio.Task[None] | None = None
        # E3: hold references to unload-triggered kill background tasks so GC
        # does not collect them and leave processes uncleaned
        self._pending_kill_tasks: set[asyncio.Task] = set()
        # R2: inline concurrency gate limiting the number of concurrent plugin
        # executions in the to_thread pool
        self._concurrency_sem: asyncio.Semaphore | None = None
        # P2-5: idempotency cache preventing duplicate side effects (double
        # execution/accounting) from client reconnect retries.
        # key=(plugin_id, idempotency_key); repeat requests within the TTL
        # return the cached result directly.
        self._idem_cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self._idem_lock = threading.Lock()
        self._IDEM_TTL = 600  # seconds, aligned with DEFAULT_TIMEOUT
        # Observability: register the active plugins gauge at startup (initial
        # value 0) so /metrics always has this metric line
        self.desk.metrics.gauge("active_plugins", "number of plugins currently ENABLED").set(0)

    # ── A2: config-driven thresholds, falling back to class constants when
    # config is missing (compatible with test paths without config) ──

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

    # Lazily create the concurrency semaphore (needs an event loop, hence
    # deferred construction).
    def _concurrency_semaphore(self) -> asyncio.Semaphore:
        if self._concurrency_sem is None:
            self._concurrency_sem = asyncio.Semaphore(self._cfg_max_concurrent())
        return self._concurrency_sem

    # Perform a state transition (with validity checking).
    # Invalid transitions raise immediately and keep the original state,
    # avoiding silent entry into an inconsistent state.
    def _transition(self, inst: PluginInstance, new_state: PluginState) -> None:
        allowed = self._VALID_TRANSITIONS.get(inst.state, set())
        if new_state not in allowed:
            logger.error(
                "lifecycle: illegal state transition %s → %s (plugin=%s), rejecting",
                inst.state.value,
                new_state.value,
                inst.manifest.id,
            )
            # P2-9: illegal state transitions funnel into the desk ring buffer,
            # queryable by ops via plugins/logs.stream
            try:
                self.registry.desk.infra_log(
                    "lifecycle",
                    "ERROR",
                    f"illegal state transition {inst.state.value} → {new_state.value} (plugin={inst.manifest.id})",
                )
            except Exception:
                pass
            raise RuntimeError(
                f"illegal state transition {inst.state.value} → {new_state.value} "
                f"(plugin={inst.manifest.id})"
            )
        inst.state = new_state
        inst.start_time = time.time()

    def load(
        self, plugin_id: str, _loading_chain: frozenset[str] | None = None
    ) -> PluginInstance:
        # Load a plugin (instantiate entry_point), loading dependencies first.
        with self._instances_lock:
            manifest = self.registry.get(plugin_id)
            if manifest is None:
                raise KeyError(f"plugin {plugin_id!r} not registered")

            if plugin_id in self._instances:
                return self._instances[plugin_id]

            # Load dependencies recursively
            chain = _loading_chain or frozenset()
            if plugin_id in chain:
                raise ValueError(f"circular dependency: {' → '.join(chain)} → {plugin_id}")
            next_chain = chain | {plugin_id}
            for dep_id in manifest.depends_on:
                if dep_id not in self._instances:
                    self.load(dep_id, _loading_chain=next_chain)

            # Resolve entry_point
            entry = manifest.entry_point
            if isinstance(entry, str):
                module_path, _, attr = entry.partition(":")
                mod = importlib.import_module(module_path)
                entry = getattr(mod, attr) if attr else mod
            # Instantiation logic:
            # - class (type) → instantiate via entry()
            # - plain function/callable → use entry directly (contract signature (desk, params))
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
            self.desk.log(plugin_id, "INFO", "plugin loaded")
            return plugin_inst

    # Enable a plugin (acquire vRAM + mark running).
    # vRAM acquisition failure raises RuntimeError (instead of returning a
    # CRASHED instance) so callers clearly perceive the failure.
    async def enable(self, plugin_id: str) -> PluginInstance:
        with self._instances_lock:
            inst = self._instances.get(plugin_id) or self.load(plugin_id)
            manifest = inst.manifest

            # Acquire vRAM (solves the "vRAM preemption conflict" pain point)
            if manifest.vram_mb > 0:
                ok = self.desk.acquire_vram(plugin_id, manifest.vram_mb)
                if not ok:
                    self._transition(inst, PluginState.CRASHED)
                    self.desk.log(plugin_id, "ERROR", "vRAM request failed, plugin not enabled")
                    raise RuntimeError(f"plugin {plugin_id!r} vRAM request failed, not enabled")

            self._transition(inst, PluginState.ENABLED)
            inst.last_heartbeat = time.time()
            self.desk.log(plugin_id, "INFO", "plugin enabled")
            self._update_active_gauge()
            # Step 4: sync plugin enabled state to cluster shared storage (cross-node consistency)
            from fusion_plugins_ecosystem import cluster_bridge

            cluster_bridge.record_plugin_state(plugin_id, installed=True, enabled=True)
            return inst

    # Disable a plugin (release vRAM + terminate sandbox process).
    async def disable(self, plugin_id: str) -> None:
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
        self.desk.log(plugin_id, "INFO", "plugin disabled")
        self._update_active_gauge()
        # Step 4: sync plugin disabled state to cluster shared storage (cross-node consistency)
        from fusion_plugins_ecosystem import cluster_bridge

        cluster_bridge.record_plugin_state(plugin_id, installed=True, enabled=False)

    # Sync the active plugins gauge (count of ENABLED instances).
    def _update_active_gauge(self) -> None:
        with self._instances_lock:
            active = sum(
                1 for i in self._instances.values() if i.state == PluginState.ENABLED
            )
        self.desk.metrics.gauge("active_plugins", "number of plugins currently ENABLED").set(active)

    # Unload a plugin instance (registration retained).
    # In PROCESS mode also kills the worker to avoid orphan process leftovers (P1-6).
    # kill is async, but unload's sync signature is called by _maybe_restart;
    # here we clean up the process handle synchronously: kill's internal task
    # cancel + terminate is sufficient, full reaping is left to the sandbox
    # destructor. To keep sync semantics, pop and mark directly.
    def unload(self, plugin_id: str) -> None:
        with self._instances_lock:
            inst = self._instances.pop(plugin_id, None)
        if inst is None:
            return
        if inst.manifest.vram_mb > 0:
            self.desk.release_vram(plugin_id)
        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            # Cannot await kill in sync context; schedule async cleanup on the
            # event loop.
            # E3: hold the task reference to prevent GC collection; removed
            # automatically when done.
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._sandbox.kill(plugin_id))
                self._pending_kill_tasks.add(task)
                task.add_done_callback(self._pending_kill_tasks.discard)
            except RuntimeError:
                # No running event loop (sync test path); best-effort terminate
                logger.debug("lifecycle: unload has no event loop, skipping PROCESS kill")
        self.desk.log(plugin_id, "INFO", "plugin unloaded")

    async def execute(
        self,
        plugin_id: str,
        params: dict[str, Any],
        timeout_override: int | None = None,
    ) -> Any:
        # Execute a plugin with timeout meltdown.
        # Addresses the "subagent runs 40 minutes with zero token consumption
        # and no logs" pain point:
        # - past timeout_seconds force-terminate (inline can truly interrupt via
        #   to_thread; PROCESS kills the process)
        # - heartbeat-based stuck detection only applies to the PROCESS sandbox
        #   (it has an independent heartbeat thread)
        # P2-5: idempotency key dedup. Client reconnect retries of tools/call
        # would execute a non-idempotent plugin twice (double side effects /
        # accounting). With idempotency_key present, a hit within the TTL
        # returns the cached result and skips execution. The key is popped from
        # params, not passed through to the plugin entry point.
        idem_key = params.pop("_idempotency_key", None) if params else None
        if idem_key:
            cache_key = (plugin_id, str(idem_key))
            now = time.time()
            with self._idem_lock:
                hit = self._idem_cache.get(cache_key)
                if hit and now - hit[0] < self._IDEM_TTL:
                    self.desk.infra_log(
                        "lifecycle",
                        "INFO",
                        f"idempotent hit plugin={plugin_id} key={idem_key}, skipping duplicate execution",
                    )
                    self.desk.metrics.counter(
                        "plugin_executions_total", "plugin executions total (bucketed by result)"
                    ).inc(plugin=plugin_id, status="idempotent_hit")
                    return hit[1]
                # Purge expired entries (lazily, avoiding a dedicated sweeper thread)
                stale = [
                    k
                    for k, (ts, _) in self._idem_cache.items()
                    if now - ts >= self._IDEM_TTL
                ]
                for k in stale:
                    del self._idem_cache[k]

        with self._instances_lock:
            inst = self._instances.get(plugin_id)
            if inst is None or inst.state != PluginState.ENABLED:
                raise RuntimeError(f"plugin {plugin_id!r} not enabled, cannot execute")
            timeout = (
                timeout_override or inst.manifest.timeout_seconds or self._cfg_timeout()
            )
            inst.last_heartbeat = time.time()

        try:
            result = await asyncio.wait_for(
                self._invoke(inst, params),
                timeout=timeout,
            )
            # P1-1: a successful execution resets the crash budget so that
            # occasional crashes of long-lived plugins cannot accumulate to a
            # permanent CRASHED state.
            # Only "consecutive failures" should accumulate restart_count; one
            # success clears it.
            with self._instances_lock:
                if inst.restart_count:
                    inst.restart_count = 0
            # P2-5: store the successful result in the idempotency cache for
            # reconnect retries
            if idem_key:
                with self._idem_lock:
                    self._idem_cache[(plugin_id, str(idem_key))] = (time.time(), result)
            self.desk.metrics.counter(
                "plugin_executions_total", "plugin executions total (bucketed by result)"
            ).inc(plugin=plugin_id, status="success")
            return result
        except TimeoutError:
            # PROCESS mode: timeout must kill the worker, otherwise orphan
            # processes linger and a restart reuses the stuck process (P1-6)
            if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
                await self._sandbox.kill(plugin_id)
            with self._instances_lock:
                self._transition(inst, PluginState.TIMEOUT)
                inst.error_count += 1
                inst.last_error = f"execution timed out ({timeout}s)"
            self.desk.log(plugin_id, "ERROR", "plugin execution timed out, meltdown triggered", timeout=timeout)
            self.desk.metrics.counter(
                "plugin_executions_total", "plugin executions total (bucketed by result)"
            ).inc(plugin=plugin_id, status="timeout")
            self.desk.metrics.counter(
                "plugin_errors_total", "plugin execution errors total (bucketed by type)"
            ).inc(plugin=plugin_id, kind="timeout")
            await self._maybe_restart(plugin_id)
            raise
        except Exception as exc:
            with self._instances_lock:
                self._transition(inst, PluginState.CRASHED)
                inst.error_count += 1
                inst.last_error = str(exc)
            self.desk.log(plugin_id, "ERROR", "plugin execution crashed", error=str(exc))
            self.desk.metrics.counter(
                "plugin_executions_total", "plugin executions total (bucketed by result)"
            ).inc(plugin=plugin_id, status="error")
            self.desk.metrics.counter(
                "plugin_errors_total", "plugin execution errors total (bucketed by type)"
            ).inc(plugin=plugin_id, kind=type(exc).__name__)
            # P3-3: load-time not-callable is a config error; restarting is
            # pointless and wastes budget, so skip auto-restart
            if not isinstance(exc, PluginLoadError):
                await self._maybe_restart(plugin_id)
            raise

    # Actually invoke the plugin entry point. Picks in-process or sandbox
    # execution based on sandbox_mode.
    # sandbox_mode selection priority:
    # 1. manifest explicitly declares PROCESS → sandbox execution
    # 2. manifest defaults to INLINE but EcosystemConfig.sandbox_default_mode=process → sandbox execution
    # 3. otherwise → in-process execution
    # When config is missing, defaults to inline (consistent with the many
    # config-less call paths/tests), but an explicitly injected invalid
    # sandbox_default_mode must raise — no more getattr fallback (P2-4).
    async def _invoke(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        if inst.manifest.sandbox_mode == SandboxMode.PROCESS:
            return await self._invoke_sandbox(inst, params)

        if self.config is None:
            return await self._invoke_inline(inst, params)

        config_default = self.config.sandbox_default_mode
        if config_default not in ("inline", "process"):
            raise RuntimeError(
                f"lifecycle: illegal sandbox_default_mode={config_default!r}"
            )
        if config_default == "process":
            return await self._invoke_sandbox(inst, params)

        return await self._invoke_inline(inst, params)

    # Invoke the plugin entry point in-process.
    # Sync entry points are pushed to a thread pool via to_thread to avoid
    # blocking the event loop and to make wait_for timeouts actually effective (P1-2).
    # R2: the semaphore limits concurrent inline executions, preventing an
    # unbounded thread pool from exhausting resources.
    async def _invoke_inline(self, inst: PluginInstance, params: dict[str, Any]) -> Any:
        instance = inst.instance
        if inspect.iscoroutinefunction(instance):
            async with self._concurrency_semaphore():
                return await instance(self.desk, params)
        if callable(instance):
            async with self._concurrency_semaphore():
                return await asyncio.to_thread(instance, self.desk, params)
        raise PluginLoadError(f"entry point of plugin {inst.manifest.id!r} is not callable")

    async def _invoke_sandbox(
        self, inst: PluginInstance, params: dict[str, Any]
    ) -> Any:
        # Invoke the plugin entry point in a sandbox process.
        plugin_id = inst.manifest.id
        if self._sandbox.health(plugin_id) != "alive":
            # E1: pass the real plugin config instead of an empty dict so the
            # worker can read runtime parameters
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

    # Auto-restart after a crash (limited to max_restart attempts; plugin-level
    # config takes priority).
    # R3: exponential backoff before restarting (2^n seconds, capped at 60s)
    # avoids a crash loop instantly saturating the CPU; enable exceptions are
    # caught so a failed restart does not raise a second error to the caller,
    # which would recurse into meltdown.
    async def _maybe_restart(self, plugin_id: str) -> None:
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
                self.desk.log(plugin_id, "ERROR", "max restart count reached, plugin stays crashed")
                return
            # Save the count; unload will delete the old instance
            new_count = inst.restart_count + 1
        # R3: exponential backoff to avoid an instant crash-loop restart
        # saturating resources.
        # Config-less test paths do not back off (preserving the original
        # immediate-restart semantics); production backs off by 2^n.
        backoff = 0
        if self.config is not None:
            backoff = min(2 ** (new_count - 1), 60)
        if backoff > 0:
            await asyncio.sleep(backoff)
            # The plugin may have been disabled/unloaded during backoff; confirm again
            with self._instances_lock:
                if plugin_id not in self._instances:
                    return
        self.unload(plugin_id)
        new_inst = self.load(plugin_id)
        with self._instances_lock:
            # P2-7: inside the write lock, confirm this is still the current
            # instance to avoid concurrent double restarts clobbering counts.
            # new_inst was produced by this load; if replaced by another
            # unload/load path, skip the write.
            current = self._instances.get(plugin_id)
            if current is new_inst:
                new_inst.restart_count = new_count
            elif current is not None:
                # Replaced by a concurrent restart; adopt its count +1 to keep
                # the crash budget accumulation semantics
                current.restart_count = max(current.restart_count, new_count)
        try:
            await self._enable_with_retry(plugin_id)
            self.desk.log(plugin_id, "INFO", "plugin auto-restarted", count=new_count)
        except Exception as exc:
            # R3: a failed restart-enable does not raise to the caller; record
            # the crashed state to avoid recursion
            with self._instances_lock:
                self._transition(new_inst, PluginState.CRASHED)
                new_inst.last_error = f"auto-restart failed: {exc}"
            self.desk.log(plugin_id, "ERROR", "plugin auto-restart failed", error=str(exc))

    async def _enable_with_retry(
        self, plugin_id: str, max_attempts: int = 3, backoff: float = 1.0
    ) -> PluginInstance:
        # enable with transient-failure retry (P3-6c).
        # Only transient errors like vRAM acquisition failure are retried
        # (vRAM contention may free up momentarily); other errors (state
        # transition/config) raise immediately without wasting budget.
        # Wall-clock retries do not consume the max_restart budget (that is the
        # crash restart budget; enable transient retries are independent).
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self.enable(plugin_id)
            except RuntimeError as exc:
                last_exc = exc
                # Only vRAM request failure counts as transient; other
                # RuntimeErrors (state transition/config) are not retried
                if "vRAM request failed" not in str(exc):
                    raise
                self.desk.log(
                    plugin_id,
                    "WARNING",
                    "enable transient failure (vRAM contention), retrying",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff * attempt)
        assert last_exc is not None
        raise last_exc

    # Start the anomaly detection loop, melting down stuck plugins automatically.
    async def start_watcher(self) -> None:
        if self._watcher_task is not None:
            return
        self._watcher_task = asyncio.create_task(self._watch_loop())

    # Stop the anomaly detection loop.
    async def stop_watcher(self) -> None:
        if self._watcher_task is not None:
            self._watcher_task.cancel()
            self._watcher_task = None

    # Heartbeat detection loop.
    # Only applies to PROCESS sandbox plugins (they have an independent
    # heartbeat thread that genuinely reflects process liveness).
    # inline plugins are managed separately by execute's wait_for timeout;
    # their heartbeat cannot update during execution (the event loop is
    # blocked), so watcher intervention would falsely kill legitimate long
    # tasks (P2-3).
    # The stuck threshold uses manifest.timeout_seconds, aligned with the
    # LONG_TASK capability semantics.
    async def _watch_loop(self) -> None:
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
                    threshold = (
                        inst.manifest.timeout_seconds or self._cfg_heartbeat_stale()
                    )
                    if now - inst.last_heartbeat > threshold:
                        with self._instances_lock:
                            if inst.state != PluginState.ENABLED:
                                continue
                            self._transition(inst, PluginState.TIMEOUT)
                        self.desk.log(plugin_id, "ERROR", "heartbeat timed out, judged stuck, meltdown triggered")
                        await self._sandbox.kill(plugin_id)
                        await self._maybe_restart(plugin_id)
                # P3-2: integer-division floor max(1, ...) avoids a busy
                # sleep(0) spin when heartbeat_stale=1
                await asyncio.sleep(max(1, self._cfg_heartbeat_stale() // 2))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # P0-3: a single scan/kill exception must not kill the whole
                # watchdog; log and continue
                logger.error("lifecycle: _watch_loop iteration error: %s", exc, exc_info=True)
                await asyncio.sleep(max(1, self._cfg_heartbeat_stale() // 2))

    # Return state snapshots of all plugin instances (for consumer queries).
    def list_states(self) -> list[dict[str, Any]]:
        return [inst.to_dict() for inst in self._instances.values()]

    # Query one plugin's state snapshot; returns None when not loaded.
    def get_state(self, plugin_id: str) -> dict[str, Any] | None:
        inst = self._instances.get(plugin_id)
        return inst.to_dict() if inst else None

    # Return plugin snapshots filtered by state.
    def list_by_state(self, state: PluginState) -> list[dict[str, Any]]:
        return [
            inst.to_dict() for inst in self._instances.values() if inst.state == state
        ]
