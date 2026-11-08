from __future__ import annotations

import json
import random
import statistics

import pytest

from helpers import T0, fill, row
from ispq import analysis
from ispq.analysis import build_report, build_timeline, find_outages, is_down, jitter, month_range, percentile
from ispq.store import Store

# ---- statistics ----


def test_percentile_matches_the_standard_library() -> None:
    rng = random.Random(1)
    for n in (2, 3, 10, 101, 1000):
        xs = [rng.lognormvariate(3, 0.5) for _ in range(n)]
        cuts = statistics.quantiles(xs, n=100, method="inclusive")
        for p in (1, 25, 50, 75, 95, 99):
            assert percentile(xs, p) == pytest.approx(cuts[p - 1])
    assert percentile([5.0], 50) == 5.0 and percentile([1, 2], 100) == 2 and percentile([1, 2], 0) == 1
    assert percentile([1, 2, 3, 4], 50) == 2.5


def test_percentile_rejects_nonsense() -> None:
    for args in (([], 50), ([1.0], -1), ([1.0], 101)):
        with pytest.raises(ValueError):
            percentile(*args)


def test_jitter() -> None:
    assert jitter([]) is None and jitter([10.0]) is None
    assert jitter([10, 10, 10]) == 0
    assert jitter([10, 20, 10, 20]) == 10
    assert jitter([1, 2, 4]) == pytest.approx(1.5)


# ---- down, gaps, outages ----


def test_is_down() -> None:
    assert is_down([row(0, 0, "a"), row(0, 0, "b")])
    assert not is_down([row(0, 0, "a"), row(0, 1, "b")])  # one server dead is not an outage
    assert not is_down([row(0, 5, "a")]) and not is_down([])
    # a single lost packet out of five is not down; a DNS failure alone is not either
    assert not is_down([row(0, 4, "a"), row(0, 0, "d", kind="dns")])
    assert is_down([row(0, 0, "d", kind="dns")])  # without TCP targets, any kind counts
    assert not is_down([row(0, 0, "x", sent=0)])  # nothing was attempted


def timeline(pattern: str, interval: float = 60.0) -> analysis.Timeline:
    s = Store(":memory:")
    fill(s, pattern, interval)
    tl = build_timeline(s.rows(0, T0 * 2), interval)
    s.close()
    assert tl is not None
    return tl


def outages(pattern: str, min_down: int = 2) -> list[tuple[int, int, str]]:
    tl = timeline(pattern)
    out, _ = find_outages(tl, min_down)
    return [(round((o.start - T0) / 60), o.cycles, o.ended_by) for o in out]


def test_outage_start_cycles_and_end_reason() -> None:
    assert outages("....xxx....") == [(4, 3, "recovery")]
    assert outages("xxx....") == [(0, 3, "recovery")]
    assert outages("....xxx") == [(4, 3, "end of data")]
    assert outages("..xx.xxxx..") == [(2, 2, "recovery"), (5, 4, "recovery")]
    assert outages(".....") == []


def test_blips_are_not_outages_but_are_not_forgotten() -> None:
    tl = timeline("..x...xx..")
    out, blips = find_outages(tl)
    assert len(out) == 1 and len(blips) == 1 and blips[0].cycles == 1
    out, blips = find_outages(tl, min_down=1)
    assert len(out) == 2 and blips == []


def test_a_gap_is_neither_up_nor_down_and_cuts_an_outage() -> None:
    tl = timeline("..xx____xx..")  # the collector was off for four cycles in the middle of a long outage
    assert len(tl.gaps) == 1
    out, _ = find_outages(tl)
    assert [(o.cycles, o.ended_by) for o in out] == [(2, "gap"), (2, "recovery")]  # not one 8-minute outage
    assert out[0].end == out[0].start + 2 * 60  # ends one interval after its last down cycle


def test_a_short_pause_is_not_a_gap() -> None:
    assert timeline("..__..").gaps == []  # two missed cycles: within 3 intervals
    assert len(timeline("..___..").gaps) == 1


def brute_force(pattern: str, min_down: int) -> list[tuple[int, int]]:
    """Runs of 'x' not interrupted by '_' (a gap of 3+ missing cycles) or '.'; '_' of 1-2 cycles bridges."""
    out, i, n = [], 0, len(pattern)
    chars = [(i, c) for i, c in enumerate(pattern) if c != "_"]
    k = 0
    while k < len(chars):
        if chars[k][1] != "x":
            k += 1
            continue
        j = k
        while j + 1 < len(chars) and chars[j + 1][1] == "x" and chars[j + 1][0] - chars[j][0] <= 3:
            j += 1
        if j - k + 1 >= min_down:
            out.append((chars[k][0], j - k + 1))
        k = j + 1
    del i, n
    return out


def test_outages_match_a_brute_force_reference() -> None:
    rng = random.Random(3)
    for _ in range(300):
        pattern = "".join(rng.choices(".x_", weights=[6, 3, 1], k=rng.randint(2, 60)))
        if pattern.strip("_") == "" or pattern[0] == "_":
            continue
        for min_down in (1, 2, 3):
            got = [(s, c) for s, c, _ in outages(pattern, min_down)]
            assert got == brute_force(pattern, min_down), (pattern, min_down)


def test_interval_is_detected_from_the_data() -> None:
    s = Store(":memory:")
    fill(s, ".....", interval=300)
    tl = build_timeline(s.rows(0, T0 * 2))
    assert tl is not None and tl.interval == 300
    assert build_timeline([]) is None
    s.close()


# ---- reports ----


def test_month_range() -> None:
    import datetime as dt

    lo, hi = month_range("2026-02")
    assert (hi - lo) / 86400 == 28
    assert (month_range("2028-02")[1] - month_range("2028-02")[0]) / 86400 == 29  # leap year
    lo, hi = month_range("2026-12")
    assert dt.datetime.fromtimestamp(hi, dt.UTC) == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
    for bad in ("2026", "2026-13", "x-y", "", "2026-1-1"):
        with pytest.raises(ValueError, match="YYYY-MM"):
            month_range(bad)


def test_report_numbers() -> None:
    s = Store(":memory:")
    fill(s, ".." + "x" * 5 + "." * 90 + "x" + "." * 2, interval=60)  # 100 cycles: one outage of 5, one blip
    r = build_report(s, T0 - 1, T0 + 100 * 60)
    assert r.cycles == 100 and r.down_cycles == 6
    assert r.availability_pct == pytest.approx(94.0)
    assert len(r.outages) == 1 and len(r.blips) == 1 and r.outages[0].seconds == 5 * 60
    assert r.coverage_pct == pytest.approx(100 * 100 / int((100 * 60 + 1) / 60), rel=0.02)
    d = r.as_dict()
    json.dumps(d)  # serialisable
    assert d["availability_pct"] == 94.0 and d["blips"] == 1
    s.close()


def test_gaps_lower_the_coverage_but_not_the_availability() -> None:
    s = Store(":memory:")
    fill(s, "." * 40 + "_" * 60 + "." * 40, interval=60)
    r = build_report(s, T0, T0 + 140 * 60)
    assert r.availability_pct == 100.0 and r.down_cycles == 0
    assert r.coverage_pct == pytest.approx(100 * 80 / 140, abs=1) and len(r.gaps) == 1
    s.close()


def test_empty_report() -> None:
    s = Store(":memory:")
    r = build_report(s, T0, T0 + 3600)
    assert r.cycles == 0 and r.availability_pct is None and r.coverage_pct == 0
    assert r.as_dict()["availability_pct"] is None
    s.close()


def test_target_statistics() -> None:
    s = Store(":memory:")
    from helpers import tcp_round

    s.add_cycle(T0, [tcp_round("a", 5, rtt=10), tcp_round("b", 0)])
    r = build_report(s, T0, T0 + 60)
    a, b = r.targets["a"].summary(), r.targets["b"].summary()
    assert (a["loss_pct"], a["rtt_p50"], a["rtt_max"]) == (0.0, 12.0, 14.0) and a["jitter_mean"] == 1.0
    assert b["loss_pct"] == 100.0 and "rtt_p50" not in b
    s.close()


def test_daily_series_splits_by_utc_day() -> None:
    s = Store(":memory:")
    day = analysis.month_range("2026-11")[0]
    fill(s, ".x.", interval=60, start=day + 86400 - 120)  # two cycles on day 1 (".x"), one on day 2
    r = build_report(s, day, day + 5 * 86400)
    assert [d["day"] for d in r.daily] == ["2026-11-01", "2026-11-02"]
    assert r.daily[0]["cycles"] == 2 and r.daily[0]["down_cycles"] == 1 and r.daily[0]["availability_pct"] == 50.0
    assert r.daily[1]["availability_pct"] == 100.0
    s.close()
