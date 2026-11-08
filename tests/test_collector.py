from __future__ import annotations

import threading
from pathlib import Path

import pytest

from conftest import FakeDns
from ispq import collector
from ispq.config import Config, DnsTarget, HttpTarget, TcpTarget
from ispq.store import Store


def cfg(tcp_port: int, dns: FakeDns, http_base: str, interval: float = 0.05, every: int = 1) -> Config:
    return Config(
        interval=interval,
        tcp=(TcpTarget("tcp", "127.0.0.1", tcp_port, count=2, timeout=0.5, gap=0),),
        dns=(DnsTarget("dns", "127.0.0.1", "example.com", dns.port, count=2, timeout=0.5, gap=0),),
        http=(HttpTarget("web", f"{http_base}/ok", timeout=1.0, every=every),),
    )


def test_a_cycle_runs_every_probe(tcp_port: int, dns: FakeDns, http_base: str) -> None:
    rounds = collector.run_cycle(cfg(tcp_port, dns, http_base))
    assert sorted((r.kind, r.target, r.ok) for r in rounds) == [
        ("dns", "dns", 2),
        ("http", "web", 1),
        ("tcp", "tcp", 2),
    ]


def test_http_runs_every_nth_cycle(tcp_port: int, dns: FakeDns, http_base: str) -> None:
    c = cfg(tcp_port, dns, http_base, every=3)
    kinds = [sorted(r.kind for r in collector.run_cycle(c, i)) for i in range(4)]
    assert ["http" in k for k in kinds] == [True, False, False, True]


def test_probes_run_side_by_side(dns: FakeDns, http_base: str, tcp_port: int) -> None:
    import time

    dns.delay = 0.3
    c = Config(
        dns=tuple(DnsTarget(f"d{i}", "127.0.0.1", "example.com", dns.port, count=1, timeout=2, gap=0) for i in range(4))
    )
    t = time.perf_counter()
    rounds = collector.run_cycle(c)
    assert len(rounds) == 4 and all(r.ok for r in rounds)
    assert time.perf_counter() - t < 0.3 * 4 * 0.75  # 4 x 0.3 s one after the other would be 1.2 s


def test_run_saves_cycles(tcp_port: int, dns: FakeDns, http_base: str, store: Store) -> None:
    seen: list[float] = []
    n = collector.run(
        cfg(tcp_port, dns, http_base), store, threading.Event(), max_cycles=3, on_cycle=lambda ts, rs: seen.append(ts)
    )
    assert n == 3 and store.count_cycles() == 3 and len(seen) == 3
    assert seen == sorted(seen) and len(store.rows(0, 2e10)) == 9


def test_run_stops_when_asked(tcp_port: int, dns: FakeDns, http_base: str, tmp_path: Path) -> None:
    stop = threading.Event()
    c = cfg(tcp_port, dns, http_base, interval=30)  # would wait 30 s between cycles
    db = tmp_path / "stop.db"

    def work() -> None:
        s = Store(db)  # a SQLite connection belongs to the thread that made it
        try:
            collector.run(c, s, stop)
        finally:
            s.close()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    reader = Store(db)
    for _ in range(100):
        if reader.count_cycles():
            break
        threading.Event().wait(0.05)
    stop.set()
    t.join(timeout=3)
    assert not t.is_alive() and reader.count_cycles() == 1  # woke up at once, did not wait out the interval
    reader.close()


def test_overrun_skips_missed_slots_instead_of_bursting(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    now = [1000.0]
    monkeypatch.setattr(
        collector, "run_cycle", lambda c, i=0: (now.__setitem__(0, now[0] + 25), [])[1]
    )  # each cycle takes 25 s
    waits: list[float] = []

    class Stop(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout or 0.0)
            now[0] += timeout or 0.0
            return False

    collector.run(
        Config(interval=10, tcp=(TcpTarget("t", "127.0.0.1"),)), store, Stop(), max_cycles=3, clock=lambda: now[0]
    )
    # cycle 1 ends at 1025 (due 1010): the next slot on the grid is 1030, so wait 5 s; never a negative or burst
    assert waits[0] == pytest.approx(5.0) and all(w >= 0 for w in waits)


def test_signal_handlers_set_the_event() -> None:
    import signal

    stop = threading.Event()
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        collector.install_signal_handlers(stop)
        signal.raise_signal(signal.SIGTERM)
        assert stop.is_set()
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def test_database_path(tmp_path: Path) -> None:
    assert Config(database=str(tmp_path / "x.db")).database.endswith("x.db")
