"""From raw rounds to answers: percentiles, jitter, outages, availability, a monthly report.

Definitions (they matter, so they are written down):

- A cycle is **down** when every TCP target failed every attempt (no TCP targets: when every target of any kind
  failed). One dead server is not an outage of the connection.
- An **outage** is at least `min_down` consecutive down cycles. Its start is the first down cycle; its end is the first
  cycle that is up again. Precision is therefore one interval. Shorter runs are **blips**: counted in the availability,
  listed separately.
- A **gap** is a hole in the data larger than `gap_factor` intervals (the collector was not running). Gaps are neither
  up nor down: they are left out of the availability and reported as missing coverage. An outage never spans a gap.
- **Jitter** of a round is the mean absolute difference between consecutive round-trip times.
- **Availability** is the share of the cycles that were up, over the cycles that exist.
"""

from __future__ import annotations

import calendar
import datetime as dt
import itertools
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from ispq.store import Row, Store

GAP_FACTOR = 3.0
MIN_DOWN = 2


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile (0..100) by linear interpolation between ranks, like numpy's default."""
    if not values:
        raise ValueError("no values")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    xs = sorted(values)
    pos = (len(xs) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def jitter(rtts: Sequence[float]) -> float | None:
    if len(rtts) < 2:
        return None
    return sum(abs(b - a) for a, b in itertools.pairwise(rtts)) / (len(rtts) - 1)


@dataclass(frozen=True)
class Outage:
    start: float
    end: float
    cycles: int
    ended_by: str  # recovery | gap | end of data

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass
class Timeline:
    interval: float
    cycles: list[float]
    down: list[bool]
    gaps: list[tuple[float, float]] = field(default_factory=list)  # (last cycle before, first cycle after)


def is_down(rows: Sequence[Row]) -> bool:
    tcp = [r for r in rows if r.kind == "tcp"]
    pool = tcp or list(rows)
    return bool(pool) and all(r.sent > 0 and r.ok == 0 for r in pool)


def median_interval(cycles: Sequence[float]) -> float | None:
    if len(cycles) < 2:
        return None
    return statistics.median(b - a for a, b in itertools.pairwise(cycles))


def build_timeline(rows: Sequence[Row], interval: float | None = None) -> Timeline | None:
    by_cycle: dict[float, list[Row]] = defaultdict(list)
    for r in rows:
        by_cycle[r.cycle].append(r)
    cycles = sorted(by_cycle)
    if not cycles:
        return None
    step = interval or median_interval(cycles) or 60.0
    gaps = [(a, b) for a, b in itertools.pairwise(cycles) if b - a > GAP_FACTOR * step]
    return Timeline(step, cycles, [is_down(by_cycle[c]) for c in cycles], gaps)


def find_outages(tl: Timeline, min_down: int = MIN_DOWN) -> tuple[list[Outage], list[Outage]]:
    """Returns (outages, blips)."""
    outages: list[Outage] = []
    blips: list[Outage] = []
    i, n = 0, len(tl.cycles)
    while i < n:
        if not tl.down[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and tl.down[j + 1] and tl.cycles[j + 1] - tl.cycles[j] <= GAP_FACTOR * tl.interval:
            j += 1
        count = j - i + 1
        last = tl.cycles[j]
        if j + 1 >= n:
            end, how = last + tl.interval, "end of data"
        elif tl.cycles[j + 1] - last > GAP_FACTOR * tl.interval:
            end, how = last + tl.interval, "gap"
        else:
            end, how = tl.cycles[j + 1], "recovery"
        item = Outage(tl.cycles[i], end, count, how)
        (outages if count >= min_down else blips).append(item)
        i = j + 1
    return outages, blips


@dataclass
class TargetStats:
    kind: str
    target: str
    sent: int = 0
    ok: int = 0
    rtts: list[float] = field(default_factory=list)
    jitters: list[float] = field(default_factory=list)
    mbps: list[float] = field(default_factory=list)

    @property
    def loss_pct(self) -> float:
        return 100.0 * (self.sent - self.ok) / self.sent if self.sent else 0.0

    def summary(self) -> dict[str, object]:
        out: dict[str, object] = {
            "kind": self.kind,
            "target": self.target,
            "attempts": self.sent,
            "loss_pct": round(self.loss_pct, 3),
        }
        if self.rtts:
            out.update(
                rtt_min=round(min(self.rtts), 2),
                rtt_p50=round(percentile(self.rtts, 50), 2),
                rtt_p95=round(percentile(self.rtts, 95), 2),
                rtt_p99=round(percentile(self.rtts, 99), 2),
                rtt_max=round(max(self.rtts), 2),
            )
        if self.jitters:
            out["jitter_mean"] = round(statistics.fmean(self.jitters), 3)
        if self.mbps:
            out.update(
                mbps_median=round(statistics.median(self.mbps), 2),
                mbps_min=round(min(self.mbps), 2),
                downloads=len(self.mbps),
            )
        return out


def target_stats(rows: Sequence[Row]) -> dict[str, TargetStats]:
    stats: dict[str, TargetStats] = {}
    for r in rows:
        s = stats.setdefault(r.target, TargetStats(r.kind, r.target))
        s.sent += r.sent
        s.ok += r.ok
        s.rtts.extend(r.rtts)
        j = jitter(r.rtts)
        if j is not None:
            s.jitters.append(j)
        if r.mbps is not None:
            s.mbps.append(r.mbps)
    return stats


@dataclass
class Report:
    start: float
    end: float
    interval: float
    cycles: int
    expected_cycles: int
    down_cycles: int
    outages: list[Outage]
    blips: list[Outage]
    gaps: list[tuple[float, float]]
    targets: dict[str, TargetStats]
    daily: list[dict[str, object]]

    @property
    def availability_pct(self) -> float | None:
        return 100.0 * (self.cycles - self.down_cycles) / self.cycles if self.cycles else None

    @property
    def coverage_pct(self) -> float:
        return min(100.0, 100.0 * self.cycles / self.expected_cycles) if self.expected_cycles else 0.0

    def as_dict(self) -> dict[str, object]:
        avail = self.availability_pct
        return {
            "period": {"start": iso(self.start), "end": iso(self.end)},
            "interval_seconds": self.interval,
            "cycles": self.cycles,
            "coverage_pct": round(self.coverage_pct, 2),
            "availability_pct": None if avail is None else round(avail, 4),
            "down_cycles": self.down_cycles,
            "outages": [
                {"start": iso(o.start), "end": iso(o.end), "seconds": round(o.seconds), "ended_by": o.ended_by}
                for o in self.outages
            ],
            "blips": len(self.blips),
            "gaps": [{"from": iso(a), "to": iso(b), "seconds": round(b - a)} for a, b in self.gaps],
            "targets": [s.summary() for s in self.targets.values()],
            "daily": self.daily,
        }


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_range(month: str) -> tuple[float, float]:
    """'2026-11' -> [start, end) in UTC."""
    try:
        y, m = (int(x) for x in month.split("-"))
        first = dt.datetime(y, m, 1, tzinfo=dt.UTC)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not a month (use YYYY-MM): {month!r}") from exc
    days = calendar.monthrange(y, m)[1]
    return first.timestamp(), (first + dt.timedelta(days=days)).timestamp()


def daily_series(rows: Sequence[Row], tl: Timeline, start: float, end: float) -> list[dict[str, object]]:
    """Per UTC day: availability and the p50/p95 TCP round-trip time. Days without data are left out."""
    day = 86400.0
    down_by_cycle = dict(zip(tl.cycles, tl.down, strict=True))
    buckets: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"cycles": [], "down": [], "rtt": []})
    for c, d in down_by_cycle.items():
        b = buckets[int((c - start) // day)]
        b["cycles"].append(c)
        b["down"].append(1.0 if d else 0.0)
    for r in rows:
        if r.kind == "tcp":
            buckets[int((r.cycle - start) // day)]["rtt"].extend(r.rtts)
    out: list[dict[str, object]] = []
    for k in sorted(buckets):
        b = buckets[k]
        n = len(b["cycles"])
        entry: dict[str, object] = {
            "day": dt.datetime.fromtimestamp(start + k * day, dt.UTC).strftime("%Y-%m-%d"),
            "cycles": n,
            "down_cycles": int(sum(b["down"])),
            "availability_pct": round(100.0 * (n - sum(b["down"])) / n, 3) if n else None,
        }
        if b["rtt"]:
            entry["rtt_p50"] = round(percentile(b["rtt"], 50), 2)
            entry["rtt_p95"] = round(percentile(b["rtt"], 95), 2)
        out.append(entry)
    return out


def build_report(
    store: Store, start: float, end: float, interval: float | None = None, min_down: int = MIN_DOWN
) -> Report:
    rows = store.rows(start, end)
    tl = build_timeline(rows, interval)
    if tl is None:
        return Report(start, end, interval or 60.0, 0, int((end - start) / (interval or 60.0)), 0, [], [], [], {}, [])
    outages, blips = find_outages(tl, min_down)
    return Report(
        start=start,
        end=end,
        interval=tl.interval,
        cycles=len(tl.cycles),
        expected_cycles=int((end - start) / tl.interval),
        down_cycles=sum(tl.down),
        outages=outages,
        blips=blips,
        gaps=tl.gaps,
        targets=target_stats(rows),
        daily=daily_series(rows, tl, start, end),
    )
