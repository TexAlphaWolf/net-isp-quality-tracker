"""Command line. Exit codes: 0 ok, 1 a problem was found (`once`: the connection is down), 2 error."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path

from ispq import __version__, analysis, collector, config, render
from ispq.probes import Round
from ispq.store import Row, Store, StoreError

OK, PROBLEM, ERROR = 0, 1, 2


class CliError(Exception):
    pass


def _cfg(a: argparse.Namespace) -> config.Config:
    return config.load(a.config)


def _db(a: argparse.Namespace, cfg: config.Config | None = None) -> str:
    if a.db:
        return str(a.db)
    return (cfg or config.load(a.config)).database


def _period(a: argparse.Namespace) -> tuple[float, float, str]:
    """The period asked for. The current month stops at now, so coverage is not understated."""
    if a.start or a.end:
        if not (a.start and a.end):
            raise CliError("give both --from and --to")
        try:
            lo = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.UTC).timestamp()
            hi = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.UTC).timestamp()
        except ValueError as exc:
            raise CliError("--from and --to take dates like 2026-11-01 (UTC)") from exc
        if hi <= lo:
            raise CliError("--to must be after --from")
        return lo, hi, f"{a.start} to {a.end}"
    month = a.month or dt.datetime.now(dt.UTC).strftime("%Y-%m")
    try:
        lo, hi = analysis.month_range(month)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    return lo, min(hi, max(lo + 1, time.time())), month


def _line(ts: float, rounds: list[Round]) -> str:
    parts = []
    for r in rounds:
        if r.ok:
            rtt = sum(r.rtts) / len(r.rtts)
            extra = f" {r.mbps:g}Mbit/s" if r.mbps is not None else ""
            parts.append(f"{r.target} {r.ok}/{r.sent} {rtt:.1f}ms{extra}")
        else:
            parts.append(f"{r.target} FAIL({r.error or '?'})")
    return f"{analysis.iso(ts)}  " + "  ".join(parts)


def cmd_init_config(a: argparse.Namespace) -> int:
    path = Path(a.path)
    if path.exists():
        raise CliError(f"{path} already exists; not overwriting it")
    path.write_text(config.DEFAULT_TOML, encoding="utf-8")
    print(f"wrote {path}")
    return OK


def cmd_once(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    started = time.time()
    rounds = collector.run_cycle(cfg)
    if not a.no_save:
        store = Store(_db(a, cfg))
        try:
            store.add_cycle(started, rounds)
        finally:
            store.close()
    down = analysis.is_down(
        [Row(started, r.kind, r.target, r.sent, r.ok, r.rtts, r.error, r.bytes, r.mbps) for r in rounds]
    )
    if a.json:
        print(json.dumps([r.__dict__ for r in rounds], indent=2))
    else:
        print(_line(started, rounds))
    return PROBLEM if down else OK


def cmd_run(a: argparse.Namespace) -> int:
    cfg = _cfg(a)
    store = Store(_db(a, cfg))
    stop = threading.Event()
    collector.install_signal_handlers(stop)
    print(
        f"measuring {len(cfg.targets())} target(s) every {cfg.interval:g} s into {store.path}; Ctrl+C to stop",
        file=sys.stderr,
    )
    try:
        collector.run(cfg, store, stop, a.cycles, None if a.quiet else lambda ts, rs: print(_line(ts, rs), flush=True))
    finally:
        store.close()
    return OK


def _store_for_reading(a: argparse.Namespace) -> Store:
    path = _db(a)
    if not Path(path).exists():
        raise CliError(f"no database at {path}; run `ispq run` first (or pass --db)")
    return Store(path)


def cmd_report(a: argparse.Namespace) -> int:
    lo, hi, title = _period(a)
    store = _store_for_reading(a)
    try:
        report = analysis.build_report(store, lo, hi, a.interval, a.min_down)
    finally:
        store.close()
    if a.format == "json":
        text = json.dumps(report.as_dict(), indent=2) + "\n"
    elif a.format == "html":
        text = render.html_page(report, title)
    else:
        text = render.markdown(report, title)
    if a.output and a.output != "-":
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"wrote {a.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return PROBLEM if report.outages else OK


def cmd_outages(a: argparse.Namespace) -> int:
    lo, hi, _ = _period(a)
    store = _store_for_reading(a)
    try:
        tl = analysis.build_timeline(store.rows(lo, hi), a.interval)
    finally:
        store.close()
    if tl is None:
        print("no measurements in this period")
        return OK
    outages, blips = analysis.find_outages(tl, a.min_down)
    if a.json:
        print(
            json.dumps(
                [
                    {
                        "start": analysis.iso(o.start),
                        "end": analysis.iso(o.end),
                        "seconds": round(o.seconds),
                        "ended_by": o.ended_by,
                    }
                    for o in outages
                ],
                indent=2,
            )
        )
    else:
        for o in outages:
            print(f"{analysis.iso(o.start)}  {render.duration(o.seconds):>12}  ended by {o.ended_by}")
        print(f"{len(outages)} outage(s), {len(blips)} short blip(s)")
    return PROBLEM if outages else OK


def cmd_export(a: argparse.Namespace) -> int:
    lo, hi, _ = _period(a)
    store = _store_for_reading(a)
    try:
        for line in render.influx_lines(store, lo, hi):
            print(line)
    finally:
        store.close()
    return OK


def cmd_status(a: argparse.Namespace) -> int:
    store = _store_for_reading(a)
    try:
        b = store.bounds()
        if b is None:
            print("the database is empty")
            return OK
        print(f"database   {store.path} ({Path(store.path).stat().st_size / 1e6:.1f} MB)")
        print(f"cycles     {store.count_cycles()}")
        print(f"first      {analysis.iso(b[0])}")
        print(f"last       {analysis.iso(b[1])}  ({render.duration(time.time() - b[1])} ago)")
        print("targets    " + ", ".join(f"{t} ({k})" for k, t in store.targets()))
    finally:
        store.close()
    return OK


def cmd_purge(a: argparse.Namespace) -> int:
    path = _db(a)
    if not Path(path).exists():
        raise CliError(f"no database at {path}")
    store = Store(path)
    try:
        n = store.purge_before(time.time() - a.older_than_days * 86400)
        store.db.execute("VACUUM")
    finally:
        store.close()
    print(f"removed {n} row(s) older than {a.older_than_days} day(s)")
    return OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ispq", description="Measure the quality of an internet connection and report on it."
    )
    p.add_argument("--version", action="version", version=f"ispq {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(s: argparse.ArgumentParser, period: bool = False) -> None:
        s.add_argument("--config", help="TOML file (default: the built-in public resolvers)")
        s.add_argument("--db", help="SQLite file (default: the one named in the config)")
        if period:
            s.add_argument("--month", help="YYYY-MM, UTC (default: this month)")
            s.add_argument("--from", dest="start", help="start date, UTC, e.g. 2026-11-01")
            s.add_argument("--to", dest="end", help="end date (exclusive), UTC")
            s.add_argument("--interval", type=float, help="seconds between cycles (default: detected from the data)")
            s.add_argument(
                "--min-down", type=int, default=analysis.MIN_DOWN, help="cycles in a row that make an outage"
            )

    s = sub.add_parser("init-config", help="write a starting configuration file")
    s.add_argument("path", nargs="?", default="ispq.toml")
    s.set_defaults(fn=cmd_init_config)

    s = sub.add_parser("once", help="one measurement cycle now")
    common(s)
    s.add_argument("--no-save", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_once)

    s = sub.add_parser("run", help="measure continuously")
    common(s)
    s.add_argument("--cycles", type=int, help="stop after this many cycles")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("report", help="a report for a month or a period")
    common(s, period=True)
    s.add_argument("--format", choices=["md", "html", "json"], default="md")
    s.add_argument("-o", "--output", help="file to write (default: standard output)")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("outages", help="list the outages")
    common(s, period=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_outages)

    s = sub.add_parser("export-influx", help="InfluxDB line protocol, for Grafana")
    common(s, period=True)
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("status", help="what the database holds")
    common(s)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("purge", help="delete old measurements")
    common(s)
    s.add_argument("--older-than-days", type=int, required=True)
    s.set_defaults(fn=cmd_purge)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (CliError, config.ConfigError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR
