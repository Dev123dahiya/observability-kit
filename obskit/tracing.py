"""Distributed tracing: spans, in-process context, and W3C trace-context headers."""
from __future__ import annotations

import contextvars
import re
import secrets
import time
from dataclasses import dataclass, field

_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("current_span", default=None)
TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass
class Span:
    name: str
    trace_id: str               # shared by every span in one request, across services
    span_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    status: str = "ok"
    attributes: dict = field(default_factory=dict)
    sampled: bool = True

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.start) - self.start

    def set(self, **attributes) -> None:
        self.attributes.update(attributes)


class Tracer:
    """Creates spans and collects finished ones.

    `with tracer.span("name"):` nests automatically: the new span's parent is
    whichever span is current in this thread or asyncio task. Context variables
    carry that, so concurrent requests do not see each other's spans.
    """

    def __init__(self, clock=time.monotonic, sample_rate: float = 1.0, id_source=secrets.token_hex) -> None:
        self.clock, self.sample_rate, self._hex = clock, sample_rate, id_source
        self.finished: list[Span] = []

    def span(self, name: str, parent: "tuple[str, str, bool] | None" = None, **attributes) -> "_ActiveSpan":
        """Start a span. `parent` is (trace_id, span_id, sampled) from an incoming request, see extract()."""
        current = _current.get()
        if current is not None:
            trace_id, parent_id, sampled = current.trace_id, current.span_id, current.sampled
        elif parent is not None:
            trace_id, parent_id, sampled = parent
        else:
            # The sampling decision is made once, at the root, and inherited, so a trace is kept or dropped whole.
            trace_id, parent_id = self._hex(16), None
            sampled = int(trace_id[:8], 16) / 0xFFFFFFFF < self.sample_rate
        return _ActiveSpan(self, Span(name, trace_id, self._hex(8), parent_id, self.clock(), attributes=dict(attributes), sampled=sampled))

    def trace(self, trace_id: str) -> list[Span]:
        return sorted((s for s in self.finished if s.trace_id == trace_id), key=lambda s: s.start)

    def render_tree(self, trace_id: str) -> str:
        spans = self.trace(trace_id)
        children: dict = {}
        for s in spans:
            children.setdefault(s.parent_id, []).append(s)
        known = {s.span_id for s in spans}
        lines: list[str] = []

        def walk(span: Span, depth: int) -> None:
            flag = "" if span.status == "ok" else f"  [{span.status}]"
            lines.append(f"{'  ' * depth}{span.name}  {span.duration * 1000:.0f} ms{flag}")
            for child in children.get(span.span_id, []):
                walk(child, depth + 1)

        for root in (s for s in spans if s.parent_id not in known):     # a remote parent is not in this process
            walk(root, 0)
        return "\n".join(lines)


class _ActiveSpan:
    def __init__(self, tracer: Tracer, span: Span) -> None:
        self.tracer, self.span = tracer, span

    def __enter__(self) -> Span:
        self._token = _current.set(self.span)
        return self.span

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.span.end = self.tracer.clock()
        if exc_type is not None:
            self.span.status = "error"
            self.span.attributes["error"] = f"{exc_type.__name__}: {exc}"
        _current.reset(self._token)
        if self.span.sampled:
            self.tracer.finished.append(self.span)
        return False                       # never swallow the exception


def current_span() -> "Span | None":
    return _current.get()


def inject(headers: dict) -> dict:
    """Add a W3C `traceparent` header for an outgoing call, so the next service continues this trace."""
    span = _current.get()
    if span is not None:
        headers["traceparent"] = f"00-{span.trace_id}-{span.span_id}-{'01' if span.sampled else '00'}"
    return headers


def extract(headers: dict) -> "tuple[str, str, bool] | None":
    """Read `traceparent` from an incoming request. A malformed header is ignored, and a new trace starts."""
    value = next((v for k, v in headers.items() if k.lower() == "traceparent"), "")
    match = TRACEPARENT.match(value.strip())
    if not match or match.group(1) == "ff" or set(match.group(2)) == {"0"} or set(match.group(3)) == {"0"}:
        return None
    return match.group(2), match.group(3), bool(int(match.group(4), 16) & 1)
