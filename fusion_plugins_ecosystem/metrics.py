"""可观测性指标采集（企业级运维）。

Prometheus 文本暴露格式，供编排器/K8s scrape：
- Counter：单调递增（调用数、错误数、spawn 数）
- Gauge：瞬时值（活跃插件数、显存占用、活跃会话）
- MetricsRegistry：线程安全聚合 + render() 输出

接入点：desk_runtime 持有单例，lifecycle/jsonrpc/sandbox 关键路径 inc/set。
暴露：transport /metrics 端点（未鉴权短路，同 /health）。
"""

from __future__ import annotations

import threading


class _Counter:
    """单调递增计数器，支持标签维度。

    同一组标签 inc 累加同桶；不同标签值各成独立桶。
    线程安全：内部锁保护 _values dict。
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """累加 amount（默认 1），labels 决定分桶维度。"""
        if amount < 0:
            raise ValueError(f"Counter {self.name} 不可递减，amount={amount}")
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        """读取指定标签桶当前值（测试用，不暴露给 Prometheus）。"""
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def total(self) -> float:
        """所有标签桶求和。"""
        with self._lock:
            return sum(self._values.values())

    def _label_key(self, labels: dict[str, str]) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> str:
        """输出该 Counter 的 Prometheus 文本行。"""
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        if not items:
            lines.append(f"{self.name} 0")
            return "\n".join(lines)
        for key, val in items:
            label_str = _format_labels(dict(key))
            lines.append(f"{self.name}{label_str} {val}")
        return "\n".join(lines)


class _Gauge:
    """瞬时值仪表，可 set/inc/dec，支持标签维度。

    用于活跃插件数、显存占用、活跃会话等可增可减的瞬时量。
    线程安全：内部锁保护 _values dict。
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        """直接设置标签桶值。"""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """增量调整（amount 可负）。"""
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        """减量调整。"""
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        """读取指定标签桶当前值（测试用）。"""
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def _label_key(self, labels: dict[str, str]) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> str:
        """输出该 Gauge 的 Prometheus 文本行。"""
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        if not items:
            lines.append(f"{self.name} 0")
            return "\n".join(lines)
        for key, val in items:
            label_str = _format_labels(dict(key))
            lines.append(f"{self.name}{label_str} {val}")
        return "\n".join(lines)


class MetricsRegistry:
    """指标注册表（线程安全）。

    持有全部 Counter/Gauge，render() 输出 Prometheus 文本暴露格式。
    经 DeskRuntime 注入各模块，单一来源。
    """

    def __init__(self) -> None:
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str) -> _Counter:
        """注册（或取回已存在）Counter。同名复用，避免重复建桶。"""
        with self._lock:
            if name not in self._counters:
                self._counters[name] = _Counter(name, help_text)
            return self._counters[name]

    def gauge(self, name: str, help_text: str) -> _Gauge:
        """注册（或取回已存在）Gauge。"""
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = _Gauge(name, help_text)
            return self._gauges[name]

    def get_counter(self, name: str) -> _Counter | None:
        with self._lock:
            return self._counters.get(name)

    def get_gauge(self, name: str) -> _Gauge | None:
        with self._lock:
            return self._gauges.get(name)

    def names(self) -> tuple[str, ...]:
        """全部已注册指标名（测试/自省用）。"""
        with self._lock:
            return tuple(sorted(self._counters)) + tuple(sorted(self._gauges))

    def render(self) -> str:
        """聚合全部指标的 Prometheus 文本暴露格式。"""
        with self._lock:
            counters = sorted(self._counters.values(), key=lambda c: c.name)
            gauges = sorted(self._gauges.values(), key=lambda g: g.name)
        blocks = [c.render() for c in counters] + [g.render() for g in gauges]
        return "\n".join(b for b in blocks if b) + "\n"


def _format_labels(labels: dict[str, str]) -> str:
    """标签 dict → Prometheus {k="v",...} 格式；空 dict 返回空串。"""
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _escape_label_value(v: str) -> str:
    """转义 label 值中的 \\n、"、\\。"""
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
