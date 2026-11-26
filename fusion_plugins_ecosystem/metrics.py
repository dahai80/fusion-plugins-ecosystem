# Observability metrics collection (enterprise ops).
# Prometheus text exposition format for orchestrator/K8s scraping:
# - Counter: monotonically increasing (call counts, error counts, spawns)
# - Gauge: instantaneous values (active plugins, VRAM usage, active sessions)
# - MetricsRegistry: thread-safe aggregation + render() output
# Integration: desk_runtime holds the singleton; lifecycle/jsonrpc/sandbox
# critical paths inc/set. Exposure: transport /metrics endpoint
# (unauthenticated short-circuit, same as /health).

from __future__ import annotations

import threading


class _Counter:
    # Monotonically increasing counter with label dimensions.
    # The same label set accumulates into the same bucket via inc; different
    # label values form separate buckets. Thread-safe: an internal lock
    # guards the _values dict.
    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        # Add amount (default 1); labels determine the bucket dimension.
        if amount < 0:
            raise ValueError(f"Counter {self.name} cannot decrease, amount={amount}")
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        # Read the current value of a labeled bucket (test helper, not
        # exposed to Prometheus).
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def total(self) -> float:
        # Sum across all labeled buckets.
        with self._lock:
            return sum(self._values.values())

    def _label_key(self, labels: dict[str, str]) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> str:
        # Render this Counter's Prometheus text lines.
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
    # Instantaneous-value gauge with set/inc/dec and label dimensions.
    # Used for quantities that can go up and down: active plugins, VRAM
    # usage, active sessions. Thread-safe: an internal lock guards the
    # _values dict.
    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        # Set the labeled bucket value directly.
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        # Adjust by amount (may be negative).
        key = self._label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        # Subtract amount.
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        # Read the current value of a labeled bucket (test helper).
        key = self._label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def _label_key(self, labels: dict[str, str]) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in labels.items()))

    def render(self) -> str:
        # Render this Gauge's Prometheus text lines.
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
    # Metrics registry (thread-safe).
    # Holds all Counters/Gauges; render() outputs the Prometheus text
    # exposition format. Injected into modules via DeskRuntime as the
    # single source.
    def __init__(self) -> None:
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str) -> _Counter:
        # Register (or fetch an existing) Counter. Same-name reuse avoids
        # duplicate buckets.
        with self._lock:
            if name not in self._counters:
                self._counters[name] = _Counter(name, help_text)
            return self._counters[name]

    def gauge(self, name: str, help_text: str) -> _Gauge:
        # Register (or fetch an existing) Gauge.
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
        # All registered metric names (test/introspection helper).
        with self._lock:
            return tuple(sorted(self._counters)) + tuple(sorted(self._gauges))

    def render(self) -> str:
        # Aggregate all metrics into Prometheus text exposition format.
        with self._lock:
            counters = sorted(self._counters.values(), key=lambda c: c.name)
            gauges = sorted(self._gauges.values(), key=lambda g: g.name)
        blocks = [c.render() for c in counters] + [g.render() for g in gauges]
        return "\n".join(b for b in blocks if b) + "\n"


def _format_labels(labels: dict[str, str]) -> str:
    # Label dict → Prometheus {k="v",...} format; empty dict returns ""
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _escape_label_value(v: str) -> str:
    # Escape \n, " and \ in label values
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
