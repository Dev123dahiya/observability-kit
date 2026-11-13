from __future__ import annotations

import json
import sys
import time

from .tracing import current_span

LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
REDACT = ("password", "secret", "token", "authorization", "api_key", "cookie")


class JsonLogger:
    """One JSON object per line, with the current trace and span ids attached.

    Those ids are what connect the three signals: a latency spike on a
    dashboard leads to a slow trace, and the trace id finds every log line
    that request wrote, on every service it touched.
    """

    def __init__(self, service: str, stream=None, level: str = "info", clock=time.time) -> None:
        self.service, self.stream, self.threshold, self.clock = service, stream or sys.stdout, LEVELS[level], clock

    def log(self, level: str, message: str, **fields) -> None:
        if LEVELS[level] < self.threshold:
            return
        record = {"ts": round(self.clock(), 3), "level": level, "service": self.service, "msg": message}
        span = current_span()
        if span is not None:
            record["trace_id"], record["span_id"] = span.trace_id, span.span_id
        for key, value in fields.items():
            record[key] = "[redacted]" if any(word in key.lower() for word in REDACT) else value
        self.stream.write(json.dumps(record, default=str) + "\n")

    def debug(self, message, **fields): self.log("debug", message, **fields)
    def info(self, message, **fields): self.log("info", message, **fields)
    def warning(self, message, **fields): self.log("warning", message, **fields)
    def error(self, message, **fields): self.log("error", message, **fields)
