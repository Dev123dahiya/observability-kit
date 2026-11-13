"""Service level objectives and burn-rate alerting.

An SLO of 99.9% allows 0.1% of requests to fail: the error budget. Burn rate
is how fast that budget is being spent. 1.0 means it runs out exactly at the
end of the period; 14.4 means a 30-day budget would be gone in about two days.

Alerting on burn rate over two windows at once (a long one and a short one)
is the approach described in Google's SRE Workbook, chapter 5. The long window
shows the problem is significant; the short window shows it is still happening,
so the alert clears soon after the fix instead of lingering for hours.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

HOUR = 3600.0


@dataclass(frozen=True)
class BurnRateAlert:
    name: str
    long_window: float      # seconds
    short_window: float
    threshold: float        # burn rate that must be exceeded in BOTH windows
    action: str             # "page" or "ticket"


# The SRE Workbook's recommended set for a 30-day objective:
# 2% of the budget in 1 hour, 5% in 6 hours (page); 10% in 3 days (ticket).
DEFAULT_ALERTS = (
    BurnRateAlert("fast-burn", 1 * HOUR, 5 * 60, 14.4, "page"),
    BurnRateAlert("slow-burn", 6 * HOUR, 30 * 60, 6.0, "page"),
    BurnRateAlert("budget-leak", 72 * HOUR, 6 * HOUR, 1.0, "ticket"),
)


class SLO:
    def __init__(self, name: str, target: float, period_days: int = 30, alerts=DEFAULT_ALERTS, resolution: float = 60.0) -> None:
        if not 0 < target < 1:
            raise ValueError("target is a fraction below 1, e.g. 0.999")
        self.name, self.target, self.period = name, target, period_days * 24 * HOUR
        self.alerts, self.resolution = tuple(alerts), resolution
        self._buckets: deque = deque()          # [bucket start time, good, bad], oldest first
        self._horizon = max([self.period] + [a.long_window for a in self.alerts])

    @property
    def budget(self) -> float:
        """Fraction of requests allowed to fail."""
        return 1 - self.target

    def record(self, now: float, good: int = 0, bad: int = 0) -> None:
        start = now - now % self.resolution
        if self._buckets and self._buckets[-1][0] == start:
            self._buckets[-1][1] += good
            self._buckets[-1][2] += bad
        else:
            self._buckets.append([start, good, bad])
        while self._buckets and self._buckets[0][0] <= now - self._horizon:
            self._buckets.popleft()

    def _totals(self, now: float, window: float) -> tuple[int, int]:
        good = bad = 0
        for start, g, b in reversed(self._buckets):
            if start <= now - window:
                break
            good, bad = good + g, bad + b
        return good, bad

    def error_rate(self, now: float, window: float) -> float:
        good, bad = self._totals(now, window)
        return bad / (good + bad) if good + bad else 0.0

    def burn_rate(self, now: float, window: float) -> float:
        return self.error_rate(now, window) / self.budget

    def budget_remaining(self, now: float) -> float:
        """Share of the period's error budget still unspent: 1.0 untouched, 0 gone, negative overspent.

        Measured against requests seen so far in the period, so early in a
        period a short outage looks large; it shrinks as good traffic accumulates.
        """
        good, bad = self._totals(now, self.period)
        allowed = (good + bad) * self.budget
        return 1.0 if allowed == 0 else 1 - bad / allowed

    def firing(self, now: float) -> list[BurnRateAlert]:
        return [a for a in self.alerts
                if self.burn_rate(now, a.long_window) > a.threshold and self.burn_rate(now, a.short_window) > a.threshold]
