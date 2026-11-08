"""SQLite storage. One row per target per cycle; the round-trip times of every attempt are kept (as JSON), so
percentiles are exact instead of averages of averages."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ispq.probes import Round

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rounds (
    cycle  REAL NOT NULL,          -- unix time (UTC) at which the cycle started: shared by all its rounds
    kind   TEXT NOT NULL,
    target TEXT NOT NULL,
    sent   INTEGER NOT NULL,
    ok     INTEGER NOT NULL,
    rtts   TEXT NOT NULL,          -- JSON list of milliseconds
    error  TEXT NOT NULL DEFAULT '',
    bytes  INTEGER,
    mbps   REAL
);
CREATE INDEX IF NOT EXISTS rounds_cycle ON rounds (cycle);
CREATE INDEX IF NOT EXISTS rounds_target ON rounds (target, cycle);
"""


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class Row:
    cycle: float
    kind: str
    target: str
    sent: int
    ok: int
    rtts: list[float]
    error: str
    bytes: int | None
    mbps: float | None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            row = self.db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise StoreError(f"{path} has schema {row['value']}, this version reads {SCHEMA_VERSION}")
        except (sqlite3.DatabaseError, ValueError) as exc:
            self.close()
            raise StoreError(f"{path} is not a usable ispq database: {exc}") from exc
        except StoreError:
            self.close()
            raise

    def close(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def add_cycle(self, cycle: float, rounds: Iterable[Round]) -> None:
        """Save all the rounds of one cycle, or none of them."""
        data = [
            (cycle, r.kind, r.target, r.sent, r.ok, json.dumps(r.rtts), r.error[:80], r.bytes, r.mbps) for r in rounds
        ]
        with self.transaction():
            self.db.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?)", data)

    def rows(self, start: float, end: float) -> list[Row]:
        """Rounds of the cycles in [start, end), oldest first."""
        cur = self.db.execute(
            "SELECT * FROM rounds WHERE cycle >= ? AND cycle < ? ORDER BY cycle, target", (start, end)
        )
        return [
            Row(
                r["cycle"],
                r["kind"],
                r["target"],
                r["sent"],
                r["ok"],
                json.loads(r["rtts"]),
                r["error"],
                r["bytes"],
                r["mbps"],
            )
            for r in cur
        ]

    def bounds(self) -> tuple[float, float] | None:
        row = self.db.execute("SELECT MIN(cycle) AS a, MAX(cycle) AS b FROM rounds").fetchone()
        return None if row["a"] is None else (float(row["a"]), float(row["b"]))

    def count_cycles(self) -> int:
        return int(self.db.execute("SELECT COUNT(DISTINCT cycle) FROM rounds").fetchone()[0])

    def targets(self) -> list[tuple[str, str]]:
        return [
            (r["kind"], r["target"]) for r in self.db.execute("SELECT DISTINCT kind, target FROM rounds ORDER BY 1, 2")
        ]

    def purge_before(self, cycle: float) -> int:
        with self.transaction():
            n = self.db.execute("DELETE FROM rounds WHERE cycle < ?", (cycle,)).rowcount
        return int(n)
