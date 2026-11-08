"""The measurement loop: every `interval` seconds, run all probes (in parallel), save one cycle."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import FrameType

from ispq.config import Config
from ispq.probes import Round, dns_probe, http_probe, tcp_probe
from ispq.store import Store


def run_cycle(cfg: Config, index: int = 0) -> list[Round]:
    """One pass over every target. The probes run side by side, so a cycle lasts as long as the slowest one."""
    jobs: list[Callable[[], Round]] = []
    for t in cfg.tcp:
        jobs.append(lambda t=t: tcp_probe(t))  # type: ignore[misc]
    for d in cfg.dns:
        jobs.append(lambda d=d: dns_probe(d))  # type: ignore[misc]
    for h in cfg.http:
        if index % h.every == 0:
            jobs.append(lambda h=h: http_probe(h))  # type: ignore[misc]
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        return list(pool.map(lambda job: job(), jobs))


def run(
    cfg: Config,
    store: Store,
    stop: threading.Event,
    max_cycles: int | None = None,
    on_cycle: Callable[[float, list[Round]], None] | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Measure until `stop` is set (or `max_cycles` is reached). Returns the number of cycles saved.

    Cycles are scheduled on a fixed grid (start + k * interval), so slow cycles do not make the schedule drift; if a
    cycle overruns, the missed slots are skipped rather than run in a burst.
    """
    done = 0
    index = 0
    next_at = clock()
    while not stop.is_set() and (max_cycles is None or done < max_cycles):
        started = clock()
        rounds = run_cycle(cfg, index)
        store.add_cycle(started, rounds)
        if on_cycle:
            on_cycle(started, rounds)
        done += 1
        index += 1
        next_at += cfg.interval
        now = clock()
        if next_at <= now:  # overran: skip the slots we missed
            next_at += cfg.interval * (int((now - next_at) // cfg.interval) + 1)
        if max_cycles is not None and done >= max_cycles:
            break
        stop.wait(max(0.0, next_at - clock()))
    return done


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)
