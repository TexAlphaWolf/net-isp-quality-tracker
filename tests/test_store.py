from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from helpers import T0, fill, tcp_round
from ispq.probes import Round
from ispq.store import Store, StoreError


def test_round_trip(store: Store) -> None:
    r = Round("http", "speed", 1, 1, [31.5], "", 1_000_000, 42.5)
    store.add_cycle(T0, [tcp_round("a", 4), r, Round("dns", "d", 3, 0, [], "timeout")])
    rows = store.rows(T0, T0 + 1)
    assert [(x.kind, x.target, x.sent, x.ok) for x in rows] == [
        ("tcp", "a", 5, 4),
        ("dns", "d", 3, 0),
        ("http", "speed", 1, 1),
    ]  # ordered by target name
    http = next(x for x in rows if x.kind == "http")
    assert (http.rtts, http.bytes, http.mbps) == ([31.5], 1_000_000, 42.5)
    assert next(x for x in rows if x.kind == "tcp").rtts == [20.0, 21.0, 22.0, 23.0]
    assert next(x for x in rows if x.kind == "dns").error == "timeout"


def test_range_is_half_open_and_ordered(store: Store) -> None:
    fill(store, "....", interval=10)
    assert len(store.rows(T0, T0 + 20)) == 4  # two cycles x two targets; the end is excluded
    assert next(r.cycle for r in store.rows(T0 + 10, T0 + 1000)) == T0 + 10
    assert store.rows(T0 + 1000, T0 + 2000) == []


def test_bounds_counts_targets(store: Store) -> None:
    assert store.bounds() is None and store.count_cycles() == 0
    fill(store, "..x.", interval=5)
    assert store.bounds() == (T0, T0 + 15) and store.count_cycles() == 4
    assert store.targets() == [("tcp", "a"), ("tcp", "b")]


def test_a_cycle_is_saved_whole_or_not_at_all(store: Store) -> None:
    store.add_cycle(T0, [tcp_round("a", 5), tcp_round("b", 5)])
    with pytest.raises(sqlite3.ProgrammingError):  # the second round cannot be stored: nothing of the cycle must remain
        store.add_cycle(T0 + 60, [tcp_round("a", 5), Round("tcp", "b", 5, 5, [], "", object())])  # type: ignore[arg-type]
    assert store.count_cycles() == 1
    with pytest.raises(RuntimeError), store.transaction():
        store.db.execute(
            "INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?)", (T0 + 1, "tcp", "a", 1, 1, "[]", "", None, None)
        )
        raise RuntimeError("boom")
    assert store.count_cycles() == 1  # rolled back


def test_long_errors_are_truncated(store: Store) -> None:
    store.add_cycle(T0, [Round("tcp", "a", 1, 0, [], "e" * 500)])
    assert len(store.rows(T0, T0 + 1)[0].error) == 80


def test_purge(store: Store) -> None:
    fill(store, "....", interval=100)
    assert store.purge_before(T0 + 150) == 4  # two cycles x two targets
    assert store.bounds() == (T0 + 200, T0 + 300)


def test_persistence_and_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    s = Store(path)
    fill(s, "..")
    s.close()
    again = Store(path)
    assert again.count_cycles() == 2
    again.db.execute("UPDATE meta SET value = '99'")
    again.close()
    with pytest.raises(StoreError, match="schema 99"):
        Store(path)
    path.unlink()  # the handle was closed: the file can go (matters on Windows)


def test_a_file_that_is_not_a_database_is_refused_cleanly(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite at all" * 100)
    with pytest.raises(StoreError, match="not a usable"):
        Store(junk)
    junk.unlink()
