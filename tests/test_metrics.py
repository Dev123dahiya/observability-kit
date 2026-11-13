import math
import random
import threading

import pytest

from obskit import Histogram, Registry, histogram_quantile


def test_counter_counts_per_label_set_and_cannot_go_down():
    c = Registry().counter("requests_total", "Requests", ("route", "status"))
    c.inc(route="/a", status="200")
    c.inc(2, route="/a", status="200")
    c.inc(route="/a", status="500")
    assert c.value(route="/a", status="200") == 3 and c.value(route="/a", status="500") == 1
    assert c.value(route="/b", status="200") == 0
    with pytest.raises(ValueError):
        c.inc(-1, route="/a", status="200")
    with pytest.raises(ValueError):
        c.inc(route="/a")                         # a missing label is a bug, not a new series


def test_gauge_moves_both_ways():
    g = Registry().gauge("queue_depth", "Depth")
    g.set(5); g.inc(); g.dec(3)
    assert g.value() == 3


def test_histogram_bucket_boundaries_are_inclusive():
    h = Histogram("latency", "Latency", buckets=(0.1, 0.5, 1.0))
    for v in (0.05, 0.1, 0.100001, 0.5, 0.9, 7.0):
        h.observe(v)
    assert h.cumulative() == [(0.1, 2), (0.5, 4), (1.0, 5), (math.inf, 6)]
    with pytest.raises(ValueError):
        Histogram("x", "x", buckets=(1.0, 0.5))


def test_prometheus_text_format_exactly():
    r = Registry()
    c = r.counter("http_requests_total", "Requests handled", ("route",))
    h = r.histogram("job_seconds", "Job time", buckets=(1.0, 5.0))
    c.inc(3, route='/say "hi"\n')
    h.observe(0.5); h.observe(2.5); h.observe(9)
    assert r.render() == (
        '# HELP http_requests_total Requests handled\n'
        '# TYPE http_requests_total counter\n'
        'http_requests_total{route="/say \\"hi\\"\\n"} 3\n'
        '# HELP job_seconds Job time\n'
        '# TYPE job_seconds histogram\n'
        'job_seconds_bucket{le="1"} 1\n'
        'job_seconds_bucket{le="5"} 2\n'
        'job_seconds_bucket{le="+Inf"} 3\n'
        'job_seconds_sum 12\n'
        'job_seconds_count 3\n')


def test_duplicate_metric_names_are_rejected():
    r = Registry()
    r.counter("a_total", "A")
    with pytest.raises(ValueError):
        r.gauge("a_total", "A again")


def test_quantile_interpolates_inside_the_bucket():
    # 100 observations spread evenly in (0, 1]: bucket (0, 0.5] holds 50, (0.5, 1] holds 50.
    buckets = [(0.5, 50), (1.0, 100), (math.inf, 100)]
    assert histogram_quantile(0.5, buckets) == 0.5
    assert histogram_quantile(0.25, buckets) == 0.25
    assert histogram_quantile(0.9, buckets) == pytest.approx(0.9)
    assert math.isnan(histogram_quantile(0.5, [(1.0, 0), (math.inf, 0)]))
    assert histogram_quantile(0.99, [(1.0, 10), (math.inf, 100)]) == 1.0      # lands in +Inf: best known is the top bound


def test_quantile_estimate_is_always_within_the_true_values_bucket():
    rng = random.Random(3)
    h = Histogram("latency", "Latency")
    values = sorted(rng.lognormvariate(-2.5, 0.9) for _ in range(5000))
    for v in values:
        h.observe(v)
    for q in (0.5, 0.9, 0.99):
        exact = values[math.ceil(q * len(values)) - 1]
        estimate = histogram_quantile(q, h.cumulative())
        upper = next(b for b in h.bounds if exact <= b)
        lower = max([0.0] + [b for b in h.bounds if b < exact])
        assert lower <= estimate <= upper, (q, exact, estimate)


def test_counter_is_thread_safe():
    c = Registry().counter("hits_total", "Hits")
    threads = [threading.Thread(target=lambda: [c.inc() for _ in range(5000)]) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert c.value() == 40000
