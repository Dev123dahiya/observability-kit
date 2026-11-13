"""Demo: a simulated checkout service, instrumented with all three signals, going through an incident.

Everything here is a simulation on a fake clock with a seeded random generator:
no network, no real service, and the latencies are drawn from a distribution,
not measured. What is real is the instrumentation code it exercises.
"""
from __future__ import annotations

import io
import random

from . import SLO, JsonLogger, Registry, Tracer, extract, histogram_quantile, inject


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def main() -> None:
    rng, clock = random.Random(7), FakeClock()
    ids = random.Random(11)
    tracer = Tracer(clock=clock, id_source=lambda n: "%0*x" % (n * 2, ids.getrandbits(n * 8)))
    log_out = io.StringIO()
    log = JsonLogger("checkout", stream=log_out, clock=clock)
    registry = Registry()
    requests = registry.counter("http_requests_total", "Requests handled", ("route", "status"))
    latency = registry.histogram("http_request_duration_seconds", "Request latency", ("route",))
    in_flight = registry.gauge("http_requests_in_flight", "Requests being handled now")
    slo = SLO("checkout-availability", target=0.999)

    def payment_service(headers: dict, fail: bool) -> None:
        """The downstream service: continues the caller's trace from the header."""
        with tracer.span("payments: POST /charge", parent=extract(headers)):
            clock.now += rng.uniform(0.02, 0.06)
            if fail:
                raise TimeoutError("card processor timed out")

    durations: list[float] = []

    def handle(fail: bool):
        in_flight.inc()
        started = clock.now
        root = None
        try:
            with tracer.span("checkout: POST /orders", route="/orders") as root:
                try:
                    with tracer.span("db: SELECT cart"):
                        clock.now += rng.uniform(0.002, 0.008)
                    with tracer.span("http: call payments"):
                        payment_service(inject({}), fail)
                    with tracer.span("db: INSERT order"):
                        clock.now += rng.uniform(0.003, 0.010)
                    log.info("order placed", password="never-logged")
                except TimeoutError as exc:
                    log.error("order failed", error=str(exc))     # logged inside the span, so it carries the ids
                    raise
            status = "200"
        except TimeoutError:
            status = "503"
        in_flight.dec()
        requests.inc(route="/orders", status=status)
        latency.observe(clock.now - started, route="/orders")
        durations.append(clock.now - started)
        slo.record(clock.now, good=status == "200", bad=status != "200")
        return root, status

    # 29 days of ordinary history, so the 30-day budget and the 3-day window have something behind them.
    RATE, DAY = 60, 86400
    for minute in range(29 * 24 * 60):
        slo.record(minute * 60.0, good=RATE, bad=1 if minute % 80 == 0 else 0)      # about 0.02% errors
    t0 = 29 * DAY

    print("Simulated: day 30 of a month, 8 hours of traffic at 60 requests a minute, against a 99.9% objective.")
    print("From 3h00 to 4h00 a bad deploy makes 5% of payments time out.\n")
    was_firing: set[str] = set()
    failed_trace = None
    for minute in range(8 * 60):
        incident = 180 <= minute < 240
        for i in range(RATE):
            clock.now = t0 + minute * 60 + i
            root, status = handle(rng.random() < (0.05 if incident else 0.0002))
            if status == "503" and incident and failed_trace is None:
                failed_trace = root.trace_id
        clock.now = t0 + minute * 60 + 59.9
        firing = {a.name for a in slo.firing(clock.now)}
        for a in slo.alerts:
            stamp = f"{(minute + 1) // 60}h{(minute + 1) % 60:02d}"
            if a.name in firing - was_firing:
                print(f"{stamp}  FIRING    {a.name:<11} ({a.action})  burn rate {slo.burn_rate(clock.now, a.long_window):.1f}x over "
                      f"{a.long_window / 3600:g}h, {slo.burn_rate(clock.now, a.short_window):.1f}x over {a.short_window / 60:g}m")
            if a.name in was_firing - firing:
                print(f"{stamp}  resolved  {a.name}")
        was_firing = firing

    total = sum(v for v in requests._series.values())
    print(f"\nAfter 8 hours: {int(total)} requests, {int(requests.value(route='/orders', status='503'))} failed, "
          f"30-day error budget remaining {slo.budget_remaining(clock.now):.0%}")
    buckets = latency.cumulative(route="/orders")
    exact = sorted(durations)
    print(f"Latency p99: {histogram_quantile(0.99, buckets) * 1000:.0f} ms estimated from histogram buckets, "
          f"{exact[int(0.99 * len(exact))] * 1000:.0f} ms exact. The gap is the bucket width (50 to 100 ms).\n")

    print("One failed request, as a trace (the payments span ran in the 'other service', joined by the traceparent header):")
    print(tracer.render_tree(failed_trace))
    print("\nIts log line, found by trace id:")
    print(next(line for line in log_out.getvalue().splitlines() if failed_trace in line))
    print("\nA successful request's log line (note the redacted field):")
    print(next(line for line in log_out.getvalue().splitlines() if '"info"' in line))
    print("\n/metrics excerpt:")
    print("\n".join(l for l in registry.render().splitlines() if "bucket" not in l or 'le="0.1"' in l or "Inf" in l))


if __name__ == "__main__":
    main()
