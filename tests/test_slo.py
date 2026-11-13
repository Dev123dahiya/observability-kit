import pytest

from obskit import SLO, BurnRateAlert, DEFAULT_ALERTS

HOUR = 3600


def feed(slo, start_min, minutes, per_min=100, bad_per_min=0):
    for m in range(start_min, start_min + minutes):
        slo.record(m * 60.0, good=per_min - bad_per_min, bad=bad_per_min)
    return (start_min + minutes) * 60.0 - 1


def test_default_alerts_spend_the_documented_share_of_a_30_day_budget():
    # burn rate x window / period = share of the budget consumed before the alert fires
    shares = {a.name: round(a.threshold * a.long_window / (30 * 24 * HOUR), 4) for a in DEFAULT_ALERTS}
    assert shares == {"fast-burn": 0.02, "slow-burn": 0.05, "budget-leak": 0.10}


def test_burn_rate_is_error_rate_over_budget():
    slo = SLO("api", 0.999)
    now = feed(slo, 0, 60, per_min=1000, bad_per_min=5)          # 0.5% errors against a 0.1% budget
    assert slo.error_rate(now, HOUR) == pytest.approx(0.005)
    assert slo.burn_rate(now, HOUR) == pytest.approx(5.0)
    assert SLO("x", 0.99).budget == pytest.approx(0.01)
    with pytest.raises(ValueError):
        SLO("bad", 99.9)                                          # a percentage passed where a fraction is expected


def test_healthy_traffic_fires_nothing():
    slo = SLO("api", 0.999)
    now = feed(slo, 0, 24 * 60)
    assert slo.firing(now) == [] and slo.budget_remaining(now) == 1.0


def test_fast_burn_needs_both_windows():
    slo = SLO("api", 0.999)
    feed(slo, 0, 6 * 60)
    now = feed(slo, 6 * 60, 3, bad_per_min=100)                   # 3 minutes of total failure: the hour is at 5% = 50x
    assert "fast-burn" in [a.name for a in slo.firing(now)]
    # A brief blip: the 5-minute window is far over the threshold, but the hour is not, so nobody is paged.
    blip = SLO("api", 0.999)
    feed(blip, 0, 6 * 60)
    now = feed(blip, 6 * 60, 1, bad_per_min=50)                   # hour: 50 / 6000 = 8.3x, below 14.4
    assert blip.burn_rate(now, 300) > 14.4
    assert "fast-burn" not in [a.name for a in blip.firing(now)]


def test_alert_clears_soon_after_the_fix_because_of_the_short_window():
    slo = SLO("api", 0.999)
    feed(slo, 0, 6 * 60)
    now = feed(slo, 360, 30, bad_per_min=10)                      # 30 minutes at 10% errors
    assert "fast-burn" in [a.name for a in slo.firing(now)]
    now = feed(slo, 390, 6)                                       # fixed; six clean minutes later
    assert slo.burn_rate(now, HOUR) > 14.4                        # the hour still looks terrible
    assert "fast-burn" not in [a.name for a in slo.firing(now)]   # but the page has stopped


def test_slow_leak_raises_a_ticket_not_a_page():
    slo = SLO("api", 0.999)
    now = feed(slo, 0, 72 * 60, per_min=1000, bad_per_min=2)      # 0.2% errors for three days = 2x burn
    firing = slo.firing(now)
    assert [(a.name, a.action) for a in firing] == [("budget-leak", "ticket")]


def test_budget_remaining_goes_negative_when_overspent():
    slo = SLO("api", 0.99, period_days=1)
    now = feed(slo, 0, 100, per_min=100, bad_per_min=1)           # exactly on budget: 1% errors
    assert slo.budget_remaining(now) == pytest.approx(0.0)
    now = feed(slo, 100, 100, per_min=100, bad_per_min=3)
    assert slo.budget_remaining(now) == pytest.approx(-1.0)       # spent twice what was allowed


def test_old_data_ages_out_of_windows_and_memory():
    slo = SLO("api", 0.999, period_days=1, alerts=(BurnRateAlert("a", HOUR, 300, 10, "page"),))
    feed(slo, 0, 10, bad_per_min=100)
    now = feed(slo, 10, 70)
    assert slo.error_rate(now, HOUR) == 0.0                       # the outage is more than an hour old
    assert slo.error_rate(now, 2 * HOUR) > 0
    now = feed(slo, 80, 24 * 60)
    assert len(slo._buckets) <= 24 * 60 and slo.budget_remaining(now) == 1.0
