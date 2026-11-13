"""Counters, gauges and histograms, rendered in the Prometheus text exposition format."""
from __future__ import annotations

import math
import threading

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: tuple, values: tuple, extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _number(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class _Metric:
    kind = ""

    def __init__(self, name: str, help: str, labels: tuple[str, ...] = ()) -> None:
        self.name, self.help, self.label_names = name, help, tuple(labels)
        self._lock = threading.Lock()
        self._series: dict[tuple, object] = {}

    def _key(self, labels: dict) -> tuple:
        if set(labels) != set(self.label_names):
            raise ValueError(f"{self.name} expects labels {self.label_names}, got {tuple(labels)}")
        return tuple(str(labels[n]) for n in self.label_names)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    """A value that only goes up: requests served, errors, bytes sent."""
    kind = "counter"

    def inc(self, amount: float = 1, **labels) -> None:
        if amount < 0:
            raise ValueError("a counter cannot decrease; use a Gauge")
        key = self._key(labels)
        with self._lock:
            self._series[key] = self._series.get(key, 0) + amount

    def value(self, **labels) -> float:
        return self._series.get(self._key(labels), 0)

    def render(self) -> list[str]:
        return self.header() + [f"{self.name}{_labels(self.label_names, k)} {_number(v)}" for k, v in sorted(self._series.items())]


class Gauge(_Metric):
    """A value that goes up and down: queue depth, connections in use, temperature."""
    kind = "gauge"

    def set(self, value: float, **labels) -> None:
        with self._lock:
            self._series[self._key(labels)] = value

    def inc(self, amount: float = 1, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._series[key] = self._series.get(key, 0) + amount

    def dec(self, amount: float = 1, **labels) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels) -> float:
        return self._series.get(self._key(labels), 0)

    render = Counter.render


class Histogram(_Metric):
    """Counts observations into fixed buckets, so percentiles can be estimated
    later and, unlike stored percentiles, added up across servers."""
    kind = "histogram"

    def __init__(self, name: str, help: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        super().__init__(name, help, labels)
        if list(buckets) != sorted(set(buckets)):
            raise ValueError("buckets must be strictly increasing")
        self.bounds = tuple(buckets) + (math.inf,)

    def observe(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            counts, total = self._series.get(key) or ([0] * len(self.bounds), 0.0)
            for i, bound in enumerate(self.bounds):
                if value <= bound:                 # an observation equal to a bound belongs in that bucket ("le")
                    counts[i] += 1
                    break
            self._series[key] = (counts, total + value)

    def cumulative(self, **labels) -> list[tuple[float, int]]:
        """[(upper bound, observations <= bound)], the shape Prometheus stores."""
        counts, _ = self._series.get(self._key(labels)) or ([0] * len(self.bounds), 0.0)
        running, out = 0, []
        for bound, count in zip(self.bounds, counts):
            running += count
            out.append((bound, running))
        return out

    def render(self) -> list[str]:
        lines = self.header()
        for key, (counts, total) in sorted(self._series.items()):
            running = 0
            for bound, count in zip(self.bounds, counts):
                running += count
                le = 'le="' + _number(bound) + '"'
                lines.append(f"{self.name}_bucket{_labels(self.label_names, key, le)} {running}")
            lines.append(f"{self.name}_sum{_labels(self.label_names, key)} {_number(round(total, 9))}")
            lines.append(f"{self.name}_count{_labels(self.label_names, key)} {running}")
        return lines


def histogram_quantile(q: float, cumulative: list[tuple[float, int]]) -> float:
    """Estimate a quantile from bucket counts the way Prometheus does.

    Finds the bucket holding the q-th observation and interpolates linearly
    inside it. The answer is therefore only as precise as the bucket is wide;
    if the quantile lands in the +Inf bucket, the highest finite bound is
    returned because nothing more is known.
    """
    if not 0 <= q <= 1:
        raise ValueError("q must be between 0 and 1")
    total = cumulative[-1][1]
    if total == 0:
        return math.nan
    rank = q * total
    previous_bound, previous_count = 0.0, 0
    for bound, count in cumulative:
        if count >= rank:
            if bound == math.inf:
                return previous_bound
            in_bucket = count - previous_count
            return bound if in_bucket == 0 else previous_bound + (bound - previous_bound) * (rank - previous_count) / in_bucket
        previous_bound, previous_count = bound, count
    return previous_bound


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}

    def register(self, metric: _Metric) -> _Metric:
        if metric.name in self._metrics:
            raise ValueError(f"metric {metric.name} is already registered")
        self._metrics[metric.name] = metric
        return metric

    def counter(self, name, help, labels=()) -> Counter:
        return self.register(Counter(name, help, labels))

    def gauge(self, name, help, labels=()) -> Gauge:
        return self.register(Gauge(name, help, labels))

    def histogram(self, name, help, labels=(), buckets=DEFAULT_BUCKETS) -> Histogram:
        return self.register(Histogram(name, help, labels, buckets))

    def render(self) -> str:
        """The body a /metrics endpoint returns."""
        return "\n".join(line for name in sorted(self._metrics) for line in self._metrics[name].render()) + "\n"
