import asyncio
import io
import json

import pytest

from obskit import JsonLogger, Tracer, extract, inject
from obskit.tracing import current_span


class Clock:
    now = 0.0
    def __call__(self): return self.now


def test_nested_spans_share_a_trace_and_link_to_their_parent():
    c = Clock()
    tracer = Tracer(clock=c)
    with tracer.span("request") as root:
        c.now = 1
        with tracer.span("db") as db:
            c.now = 3
        with tracer.span("cache") as cache:
            c.now = 3.5
    assert db.trace_id == cache.trace_id == root.trace_id
    assert db.parent_id == cache.parent_id == root.span_id and root.parent_id is None
    assert (db.duration, cache.duration, root.duration) == (2, 0.5, 3.5)
    assert [s.name for s in tracer.finished] == ["db", "cache", "request"]      # children finish first
    assert current_span() is None
    assert tracer.render_tree(root.trace_id) == "request  3500 ms\n  db  2000 ms\n  cache  500 ms"


def test_exception_marks_the_span_failed_and_still_propagates():
    tracer = Tracer()
    with pytest.raises(KeyError):
        with tracer.span("outer"):
            with tracer.span("inner"):
                raise KeyError("missing")
    inner, outer = tracer.finished
    assert inner.status == outer.status == "error" and "KeyError" in inner.attributes["error"]
    assert current_span() is None                                             # context restored after the error


def test_traceparent_round_trip_joins_two_services_into_one_trace():
    service_a, service_b = Tracer(), Tracer()
    with service_a.span("a: handle") as a:
        headers = inject({})
    assert headers["traceparent"] == f"00-{a.trace_id}-{a.span_id}-01"
    with service_b.span("b: handle", parent=extract({"TraceParent": headers["traceparent"]})) as b:
        pass
    assert b.trace_id == a.trace_id and b.parent_id == a.span_id


def test_malformed_traceparent_starts_a_new_trace_instead_of_failing():
    good = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    assert extract({"traceparent": good}) == ("ab" * 16, "cd" * 8, True)
    assert extract({"traceparent": good[:-2] + "00"})[2] is False            # caller chose not to sample
    for bad in ["", "garbage", good.upper(), "00-" + "0" * 32 + "-" + "cd" * 8 + "-01", "ff" + good[2:], good + "-extra", good[:-5]]:
        assert extract({"traceparent": bad}) is None, bad
    assert inject({}) == {}                                                  # nothing to propagate outside a span


def test_sampling_keeps_or_drops_whole_traces():
    tracer = Tracer(sample_rate=0.25)
    for _ in range(2000):
        with tracer.span("root"):
            with tracer.span("child"):
                pass
    roots = [s for s in tracer.finished if s.name == "root"]
    children = [s for s in tracer.finished if s.name == "child"]
    assert {s.trace_id for s in roots} == {s.trace_id for s in children}     # never half a trace
    assert 400 < len(roots) < 600                                            # about a quarter of 2000


def test_concurrent_tasks_do_not_see_each_others_spans():
    tracer = Tracer()

    async def request(name):
        with tracer.span(name) as root:
            await asyncio.sleep(0.01)
            with tracer.span(name + "-child") as child:
                await asyncio.sleep(0.01)
            return root, child

    async def run():
        return await asyncio.gather(*(request(f"r{i}") for i in range(5)))

    pairs = asyncio.run(run())
    assert all(child.parent_id == root.span_id and child.trace_id == root.trace_id for root, child in pairs)
    assert len({root.trace_id for root, _ in pairs}) == 5


def test_log_lines_are_json_with_trace_ids_and_redaction():
    out, tracer = io.StringIO(), Tracer()
    log = JsonLogger("api", stream=out, clock=lambda: 12.3456)
    log.debug("hidden at info level")
    log.info("outside any span", user=7)
    with tracer.span("request") as span:
        log.warning("login", Authorization="Bearer abc", api_key="k", attempt=2)
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    assert first == {"ts": 12.346, "level": "info", "service": "api", "msg": "outside any span", "user": 7}
    assert (second["trace_id"], second["span_id"]) == (span.trace_id, span.span_id)
    assert second["Authorization"] == second["api_key"] == "[redacted]" and second["attempt"] == 2
    assert "Bearer" not in out.getvalue()
