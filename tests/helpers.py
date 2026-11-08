"""Build synthetic measurements without sockets."""

from __future__ import annotations

from ispq.probes import Round
from ispq.store import Row, Store

T0 = 1_760_000_000.0  # an arbitrary start, aligned to nothing in particular


def tcp_round(target: str, ok: int, sent: int = 5, rtt: float = 20.0) -> Round:
    return Round("tcp", target, sent, ok, [rtt + i for i in range(ok)], "" if ok else "timeout")


def row(
    cycle: float, ok: int, target: str = "a", kind: str = "tcp", sent: int = 5, rtts: list[float] | None = None
) -> Row:
    return Row(cycle, kind, target, sent, ok, rtts if rtts is not None else [20.0] * ok, "", None, None)


def fill(
    store: Store, pattern: str, interval: float = 60.0, start: float = T0, targets: tuple[str, ...] = ("a", "b")
) -> list[float]:
    """One cycle per character: '.' up, 'x' down, '_' no data (the collector was off)."""
    stamps = []
    for i, ch in enumerate(pattern):
        ts = start + i * interval
        if ch == "_":
            continue
        store.add_cycle(ts, [tcp_round(t, 0 if ch == "x" else 5) for t in targets])
        stamps.append(ts)
    return stamps
