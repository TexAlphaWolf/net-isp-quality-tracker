"""Measure ispq and write docs/report.json.

1. Probes: against servers on loopback that add a known delay, drop a known share of queries, or throttle to a known
   rate. Loopback is not an ISP, but it is the only place where the truth is known exactly.
2. Analysis: against simulated months with injected outages, collector downtime ("gaps"), single-server failures and
   one-cycle blips, and the same data analysed the naive way.
3. Cost: database size and speed, on synthetic months.

    python tools/evaluate.py            # full run
    python tools/evaluate.py --quick    # smaller, for CI

Seeded: sections 1 (except the timing noise of the machine) and 2 reproduce; the timings in 3 depend on the machine.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import socket
import statistics
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeDns, Handler  # noqa: E402
from ispq import __version__, analysis, probes  # noqa: E402
from ispq.config import DnsTarget, HttpTarget, TcpTarget  # noqa: E402
from ispq.store import Row, Store  # noqa: E402

# ---- 1. probes --------------------------------------------------------------------------------


def section_probes(quick: bool) -> dict[str, object]:
    out: dict[str, object] = {}

    # DNS answer time vs the delay the server adds
    dns = FakeDns()
    rows = []
    n = 15 if quick else 40
    for delay_ms in (0, 10, 25, 50, 100, 200):
        dns.delay = delay_ms / 1000
        r = probes.dns_probe(DnsTarget("d", "127.0.0.1", "example.com", dns.port, count=n, timeout=2, gap=0))
        errs = [x - delay_ms for x in r.rtts]
        rows.append(
            {
                "injected_ms": delay_ms,
                "queries": n,
                "answered": r.ok,
                "median_measured_ms": round(statistics.median(r.rtts), 2),
                "median_error_ms": round(statistics.median(errs), 2),
                "max_error_ms": round(max(errs), 2),
            }
        )
    out["dns_latency"] = rows

    # DNS loss vs the share of queries the server drops
    dns.delay = 0.0
    rows = []
    n = 150 if quick else 400
    for p in (0.0, 0.05, 0.2, 0.5):
        dns.drop_p, dns.rng, dns.queries, dns.dropped = p, random.Random(42), 0, 0
        r = probes.dns_probe(DnsTarget("d", "127.0.0.1", "example.com", dns.port, count=n, timeout=0.08, gap=0))
        rows.append(
            {
                "nominal_loss_pct": 100 * p,
                "queries": n,
                "dropped_by_server": dns.dropped,
                "reported_lost": r.sent - r.ok,
                "reported_loss_pct": round(100 * (r.sent - r.ok) / r.sent, 2),
            }
        )
    out["dns_loss"] = rows
    dns.close()

    # HTTP: time to first byte, and download speed vs a throttled server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    rows = []
    for delay_ms in (0, 50, 200):
        vals = [
            probes.http_probe(HttpTarget("h", f"{base}/delay?ms={delay_ms}", timeout=3)).rtts[0]
            for _ in range(8 if quick else 20)
        ]
        rows.append(
            {
                "injected_ms": delay_ms,
                "median_ttfb_ms": round(statistics.median(vals), 2),
                "median_error_ms": round(statistics.median(vals) - delay_ms, 2),
            }
        )
    out["http_ttfb"] = rows
    rows = []
    for mbps in (1, 5, 20, 50):
        pause_ms = 20
        chunk = int(mbps * 1e6 / 8 * pause_ms / 1000)
        size = chunk * 40
        vals = []
        for _ in range(3 if quick else 6):
            r = probes.http_probe(
                HttpTarget(
                    "h",
                    f"{base}/ok?size={size}&chunk={chunk}&pause_ms={pause_ms}",
                    timeout=10,
                    bandwidth=True,
                    max_bytes=size,
                )
            )
            if r.mbps is not None:
                vals.append(r.mbps)
        med = statistics.median(vals)
        rows.append(
            {
                "offered_mbps": mbps,
                "median_measured_mbps": round(med, 2),
                "error_pct": round(100 * (med - mbps) / mbps, 2),
                "downloads": len(vals),
            }
        )
    out["http_bandwidth"] = rows
    srv.shutdown()
    srv.server_close()

    # TCP connect on loopback: the floor of the measurement, and a closed port
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(128)
    lst.settimeout(0.1)
    done = threading.Event()

    def accept_all() -> None:  # a listener that never accepts fills its backlog after 128 connections
        while not done.is_set():
            try:
                lst.accept()[0].close()
            except OSError:
                continue

    acceptor = threading.Thread(target=accept_all, daemon=True)
    acceptor.start()
    r = probes.tcp_probe(
        TcpTarget("t", "127.0.0.1", lst.getsockname()[1], count=200 if not quick else 60, timeout=1, gap=0)
    )
    port = lst.getsockname()[1]
    done.set()
    acceptor.join()
    lst.close()
    closed = probes.tcp_probe(TcpTarget("t", "127.0.0.1", port, count=10, timeout=1, gap=0))
    out["tcp_loopback"] = {
        "attempts": r.sent,
        "ok": r.ok,
        "median_ms": round(statistics.median(r.rtts), 3),
        "p99_ms": round(analysis.percentile(r.rtts, 99), 3),
        "closed_port_ok": closed.ok,
        "closed_port_error": closed.error,
    }
    return out


# ---- 2. analysis ------------------------------------------------------------------------------

INTERVAL = 60
TARGETS = ("a", "b", "c")


def simulate_month(rng: random.Random, days: int = 30) -> dict[str, object]:
    month = days * 86400
    n = month // INTERVAL
    # outages that really happened: lognormal length (median 5 min), not overlapping
    outages: list[tuple[float, float]] = []
    for _ in range(rng.randint(3, 10)):
        d = min(6 * 3600, max(30.0, rng.lognormvariate(math.log(300), 1.0)))
        s = rng.uniform(0, month - d)
        if all(s + d + 900 < a or s > a + b + 900 for a, b in outages):
            outages.append((s, d))
    # the collector was not running: a few gaps, 10 min to 5 h
    gaps = []
    for _ in range(rng.randint(0, 3)):
        d = rng.uniform(600, 18000)
        s = rng.uniform(0, month - d)
        gaps.append((s, s + d))
    # in half of the long outages the collector also stopped for a while in the middle of it
    for s0, d0 in list(outages):
        if d0 >= 1200 and rng.random() < 0.5:
            g0 = s0 + 0.3 * d0
            gaps.append((g0, g0 + rng.uniform(300, 0.4 * d0)))
    c_dead = rng.uniform(0, month - 7200)  # one server unreachable for two hours; the connection is fine
    blips = {i for i in range(n) if rng.random() < 2e-4}  # one cycle with everything lost

    def in_outage(t: float) -> bool:
        return any(s <= t < s + d for s, d in outages)

    rows: list[Row] = []
    for i in range(n):
        t = i * INTERVAL + rng.uniform(0, 0.5)
        if any(a <= t < b for a, b in gaps):
            continue
        total_loss = in_outage(t) or i in blips
        for name in TARGETS:
            dead = total_loss or (name == "c" and c_dead <= t < c_dead + 7200)
            ok = 0 if dead else sum(rng.random() > 0.01 for _ in range(5))  # 1 % ordinary packet loss
            rows.append(
                Row(t, "tcp", name, 5, ok, [rng.lognormvariate(math.log(20), 0.3) for _ in range(ok)], "", None, None)
            )
    return {"rows": rows, "outages": outages, "gaps": gaps, "blips": blips, "n": n, "month": month}


def naive_outages(tl: analysis.Timeline) -> list[analysis.Outage]:
    """What you get by looking only at the list of cycles and ignoring that some are missing."""
    out, i, n = [], 0, len(tl.cycles)
    while i < n:
        if not tl.down[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and tl.down[j + 1]:
            j += 1
        if j - i + 1 >= analysis.MIN_DOWN:
            end = tl.cycles[j + 1] if j + 1 < n else tl.cycles[j] + tl.interval
            out.append(analysis.Outage(tl.cycles[i], end, j - i + 1, "recovery"))
        i = j + 1
    return out


def section_analysis(months: int) -> dict[str, object]:
    rng = random.Random(2026)
    detectable = found = detected_total = false_pos = 0
    start_err: list[float] = []
    dur_err: list[float] = []
    ours_err: list[float] = []
    naive_err: list[float] = []
    cut_by_gap = merged_across_gap = naive_merged = ended_by_gap = 0
    for _ in range(months):
        sim = simulate_month(rng)
        rows: list[Row] = sim["rows"]  # type: ignore[assignment]
        outages: list[tuple[float, float]] = sim["outages"]  # type: ignore[assignment]
        gaps: list[tuple[float, float]] = sim["gaps"]  # type: ignore[assignment]
        blips: set[int] = sim["blips"]  # type: ignore[assignment]
        n, month = int(sim["n"]), float(sim["month"])  # type: ignore[arg-type]
        tl = analysis.build_timeline(rows)
        assert tl is not None
        det, _ = analysis.find_outages(tl)
        cycles = tl.cycles

        def overlaps(o: analysis.Outage, s: float, d: float) -> bool:
            return o.start < s + d and s < o.end

        for s, d in outages:
            touched_by_gap = any(a < s + d and s < b for a, b in gaps)
            inside = sum(1 for c in cycles if s <= c < s + d)
            if touched_by_gap:
                cut_by_gap += 1
                continue
            if inside < 2:
                continue
            detectable += 1
            hits = [o for o in det if overlaps(o, s, d)]
            if len(hits) == 1:
                found += 1
                start_err.append(hits[0].start - s)
                dur_err.append(hits[0].seconds - d)
        detected_total += len(det)
        merged_across_gap += sum(1 for o in det if any(o.start <= a and b <= o.end for a, b in tl.gaps))
        naive_merged += sum(1 for o in naive_outages(tl) if any(o.start <= a and b <= o.end for a, b in tl.gaps))
        ended_by_gap += sum(1 for o in det if o.ended_by == "gap")
        false_pos += sum(1 for o in det if not any(overlaps(o, s, d) for s, d in outages))
        # availability against the true share of the month the line was up
        true_down = sum(min(d, month - s) for s, d in outages) + sum(
            INTERVAL for i in blips if not any(s <= i * INTERVAL < s + d for s, d in outages)
        )
        truth = 100 * (1 - true_down / month)
        up = sum(1 for x in tl.down if not x)
        ours = 100 * up / len(tl.down)
        naive = 100 * up / n  # a cycle that is missing counts as down
        ours_err.append(abs(ours - truth))
        naive_err.append(abs(naive - truth))
    return {
        "simulated_months": months,
        "outage_detection": {
            "injected_clean_outages": detectable,
            "detected_once": found,
            "recall_pct": round(100 * found / detectable, 2),
            "detected_total": detected_total,
            "false_outages": false_pos,
            "start_error_s_min_max": [round(min(start_err), 1), round(max(start_err), 1)],
            "duration_error_s_min_max": [round(min(dur_err), 1), round(max(dur_err), 1)],
            "interval_s": INTERVAL,
        },
        "outages_interrupted_by_missing_data": {
            "injected": cut_by_gap,
            "ispq_outages_that_span_missing_data": merged_across_gap,
            "outages_cut_at_the_gap": ended_by_gap,
            "naive_outages_that_span_missing_data": naive_merged,
        },
        "availability_error_points": {
            "ispq_mean": round(statistics.fmean(ours_err), 4),
            "ispq_max": round(max(ours_err), 4),
            "naive_missing_counts_as_down_mean": round(statistics.fmean(naive_err), 4),
            "naive_max": round(max(naive_err), 4),
        },
        "percentiles": section_percentiles(rng),
    }


def section_percentiles(rng: random.Random) -> dict[str, object]:
    """p95 from every sample vs the p95 of per-round averages (what you get if only averages are kept)."""
    mu, sigma = math.log(20), 0.4
    true_p95 = math.exp(mu + sigma * 1.6448536269514722)
    rounds = [[rng.lognormvariate(mu, sigma) for _ in range(5)] for _ in range(20_000)]
    samples = [x for r in rounds for x in r]
    exact = analysis.percentile(samples, 95)
    of_means = analysis.percentile([sum(r) / 5 for r in rounds], 95)
    return {
        "true_p95_ms": round(true_p95, 2),
        "from_all_samples_ms": round(exact, 2),
        "from_round_averages_ms": round(of_means, 2),
        "error_all_samples_pct": round(100 * (exact - true_p95) / true_p95, 2),
        "error_round_averages_pct": round(100 * (of_means - true_p95) / true_p95, 2),
        "samples": len(samples),
    }


# ---- 3. cost ----------------------------------------------------------------------------------


def build_database(path: Path, months: int) -> tuple[int, float]:
    """Write `months` of synthetic data (3 TCP + 1 DNS target, one cycle a minute); returns (rows, seconds)."""
    rng = random.Random(1)
    store = Store(path)
    start = time.perf_counter()
    rows = 0
    base = analysis.month_range("2026-01")[0]
    per_month = 30 * 1440
    with store.transaction():
        for c in range(months * per_month):
            ts = base + c * 60.0
            batch = []
            for kind, name in (("tcp", "a"), ("tcp", "b"), ("tcp", "c"), ("dns", "d")):
                rtts = [round(rng.lognormvariate(math.log(20), 0.3), 3) for _ in range(5 if kind == "tcp" else 3)]
                batch.append((ts, kind, name, len(rtts), len(rtts), json.dumps(rtts), "", None, None))
            store.db.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?)", batch)
            rows += len(batch)
    elapsed = time.perf_counter() - start
    store.close()
    return rows, elapsed


def section_cost(quick: bool) -> dict[str, object]:
    out: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # the real write path: one transaction per cycle
        s = Store(tmpdir / "write.db")
        from ispq.probes import Round

        cycle = [Round("tcp", t, 5, 5, [20.0] * 5) for t in "abc"] + [Round("dns", "d", 3, 3, [30.0] * 3)]
        n = 300 if quick else 1500
        t = time.perf_counter()
        for i in range(n):
            s.add_cycle(1e9 + i * 60, cycle)
        out["write_one_cycle_ms"] = round((time.perf_counter() - t) / n * 1000, 3)
        s.close()

        months = 2 if quick else 12
        db = tmpdir / "big.db"
        rows, secs = build_database(db, months)
        size = db.stat().st_size
        out["database"] = {
            "months": months,
            "rows": rows,
            "bytes": size,
            "mb_per_month": round(size / months / 1e6, 1),
            "build_seconds": round(secs, 1),
        }
        s = Store(db)
        lo, hi = analysis.month_range("2026-01")
        t = time.perf_counter()
        r = analysis.build_report(s, lo, hi)
        one = time.perf_counter() - t
        t = time.perf_counter()
        from ispq import render

        render.html_page(r, "2026-01")
        html_s = time.perf_counter() - t
        t = time.perf_counter()
        full = analysis.build_report(s, lo, analysis.month_range(f"2026-{months:02d}")[1])
        whole = time.perf_counter() - t
        out["report"] = {
            "one_month_seconds": round(one, 2),
            "one_month_cycles": r.cycles,
            "html_render_seconds": round(html_s, 3),
            "whole_database_months": months,
            "whole_database_seconds": round(whole, 2),
            "whole_database_cycles": full.cycles,
        }
        s.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "report.json"))
    ap.add_argument(
        "--only",
        choices=["probes", "analysis", "cost"],
        help="re-run one section and merge it into the existing report",
    )
    args = ap.parse_args()
    report: dict[str, object] = {
        "version": __version__,
        "python": platform.python_version(),
        "machine": f"{platform.system()} {platform.machine()}",
        "quick": args.quick,
    }
    out_path = Path(args.out)
    if args.only:
        report = json.loads(out_path.read_text(encoding="utf-8"))
    for name, fn in (
        ("probes", lambda: section_probes(args.quick)),
        ("analysis", lambda: section_analysis(40 if args.quick else 200)),
        ("cost", lambda: section_cost(args.quick)),
    ):
        if args.only and name != args.only:
            continue
        t = time.perf_counter()
        report[name] = fn()
        print(f"{name}: {time.perf_counter() - t:.1f}s", file=sys.stderr)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    a = report["analysis"]
    assert isinstance(a, dict)
    od = a["outage_detection"]
    print(
        f"outages detected: {od['detected_once']} of {od['injected_clean_outages']}, false outages: {od['false_outages']}"
    )
    interrupted = a["outages_interrupted_by_missing_data"]
    print(
        f"outages spanning missing data: ispq {interrupted['ispq_outages_that_span_missing_data']}, naive {interrupted['naive_outages_that_span_missing_data']}"
    )
    bad = od["false_outages"] or interrupted["ispq_outages_that_span_missing_data"] or od["recall_pct"] < 99
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
