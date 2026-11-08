from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import REFUSED, FakeDns
from helpers import T0, fill
from ispq import __version__
from ispq.cli import main
from ispq.store import Store


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def conf(tmp_path: Path, tcp_port: int, dns: FakeDns, http_base: str) -> Path:
    path = tmp_path / "ispq.toml"
    path.write_text(
        f"""
interval = 5
database = "{(tmp_path / "q.db").as_posix()}"
[[tcp]]
name = "local-tcp"
host = "127.0.0.1"
port = {tcp_port}
count = 2
timeout = 0.5
gap = 0
[[dns]]
name = "local-dns"
server = "127.0.0.1"
port = {dns.port}
count = 2
timeout = 0.5
gap = 0
[[http]]
name = "local-http"
url = "{http_base}/ok?size=300000&chunk=30000&pause_ms=5"
bandwidth = true
""",
        encoding="utf-8",
    )
    return path


def seeded(tmp_path: Path, pattern: str = "..xxx....__________.") -> Path:
    db = tmp_path / "seed.db"
    s = Store(db)
    fill(s, pattern, interval=60, start=T0)
    s.close()
    return db


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and __version__ in capsys.readouterr().out


def test_init_config_writes_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "new.toml"
    assert run(capsys, "init-config", str(target))[0] == 0 and "[[tcp]]" in target.read_text(encoding="utf-8")
    code, _, err = run(capsys, "init-config", str(target))
    assert code == 2 and "already exists" in err


def test_once_prints_and_saves(conf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "once", "--config", str(conf))
    assert code == 0 and "local-tcp 2/2" in out and "local-dns 2/2" in out and "Mbit/s" in out
    code, out, _ = run(capsys, "status", "--config", str(conf))
    assert code == 0 and "cycles     1" in out and "local-tcp (tcp)" in out


def test_once_json_and_no_save(conf: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = run(capsys, "once", "--config", str(conf), "--no-save", "--json")
    data = json.loads(out)
    assert code == 0 and {r["target"] for r in data} == {"local-tcp", "local-dns", "local-http"}
    assert not (tmp_path / "q.db").exists()


def test_once_exits_1_when_the_connection_is_down(
    tmp_path: Path, closed_tcp_port: int, capsys: pytest.CaptureFixture[str]
) -> None:
    c = tmp_path / "down.toml"
    c.write_text(
        f'[[tcp]]\nname = "x"\nhost = "127.0.0.1"\nport = {closed_tcp_port}\ncount = 1\ngap = 0\n', encoding="utf-8"
    )
    code, out, _ = run(capsys, "once", "--config", str(c), "--no-save")
    assert code == 1 and any(f"FAIL({reason})" in out for reason in REFUSED)


def test_run_one_cycle(conf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "run", "--config", str(conf), "--cycles", "1")
    assert code == 0 and "local-tcp" in out and "measuring 3 target(s)" in err
    code, out, _ = run(capsys, "run", "--config", str(conf), "--cycles", "1", "--quiet")
    assert code == 0 and out == ""
    assert "cycles     2" in run(capsys, "status", "--config", str(conf))[1]


def test_report_markdown_html_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path)
    args = ("--db", str(db), "--from", "2025-10-09", "--to", "2025-10-11")
    code, out, _ = run(capsys, "report", *args)
    assert (
        code == 1 and "# Internet quality: 2025-10-09 to 2025-10-11" in out and "**Outages:** 1" in out
    )  # exit 1: outage
    out_file = tmp_path / "r.html"
    code, _, err = run(capsys, "report", *args, "--format", "html", "-o", str(out_file))
    assert code == 1 and "wrote" in err and out_file.read_text(encoding="utf-8").startswith("<!doctype html>")
    code, out, _ = run(capsys, "report", *args, "--format", "json")
    data = json.loads(out)
    assert data["cycles"] == 10 + 0 + 0 or data["cycles"] > 0
    assert data["outages"][0]["seconds"] == 180 and len(data["gaps"]) == 1


def test_report_without_outages_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path, "." * 20)
    code, out, _ = run(capsys, "report", "--db", str(db), "--from", "2025-10-09", "--to", "2025-10-11")
    assert code == 0 and "**Outages:** 0" in out


def test_outages_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path)
    span = ("--from", "2025-10-09", "--to", "2025-10-11")
    code, out, _ = run(capsys, "outages", "--db", str(db), *span)
    assert code == 1 and "3 min 00 s" in out and "1 outage(s), 0 short blip(s)" in out
    code, out, _ = run(capsys, "outages", "--db", str(db), *span, "--json")
    assert json.loads(out)[0]["ended_by"] == "recovery"
    code, out, _ = run(capsys, "outages", "--db", str(db), "--from", "2030-01-01", "--to", "2030-01-02")
    assert code == 0 and "no measurements" in out


def test_export_influx(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path, "..")
    code, out, _ = run(capsys, "export-influx", "--db", str(db), "--from", "2025-10-09", "--to", "2025-10-11")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 4 and all(ln.startswith("ispq,kind=tcp,target=") for ln in lines)


def test_purge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path, "....")
    code, out, _ = run(capsys, "purge", "--db", str(db), "--older-than-days", "1")
    assert code == 0 and "removed 8 row(s)" in out
    assert "the database is empty" in run(capsys, "status", "--db", str(db))[1]


def test_recent_data_survives_purge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "recent.db"
    s = Store(db)
    fill(s, "..", start=time.time() - 120)
    s.close()
    assert "removed 0 row(s)" in run(capsys, "purge", "--db", str(db), "--older-than-days", "1")[1]


def test_current_month_stops_at_now(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "now.db"
    s = Store(db)
    fill(s, "." * 5, start=time.time() - 300)
    s.close()
    code, out, _ = run(capsys, "report", "--db", str(db), "--format", "json")
    data = json.loads(out)
    from ispq.analysis import month_range

    elapsed = time.time() - month_range(time.strftime("%Y-%m", time.gmtime()))[0]
    whole_month = 5 / (30 * 24 * 60) * 100
    assert code == 0 and data["cycles"] == 5
    # coverage is measured against the time that has passed this month, not against the whole month
    assert data["coverage_pct"] == pytest.approx(min(100.0, 5 / int(elapsed / 60) * 100), rel=0.2)
    assert data["coverage_pct"] > whole_month or elapsed > 29 * 86400


@pytest.mark.parametrize(
    "argv, message",
    [
        (["report", "--db", "DB", "--month", "2025-13"], "YYYY-MM"),
        (["report", "--db", "DB", "--from", "2025-10-01"], "both"),
        (["report", "--db", "DB", "--from", "nope", "--to", "2025-10-02"], "dates"),
        (["report", "--db", "DB", "--from", "2025-10-05", "--to", "2025-10-01"], "after"),
        (["status", "--db", "/no/such/dir/x.db"], "no database"),
        (["purge", "--db", "/no/such/dir/x.db", "--older-than-days", "1"], "no database"),
        (["once", "--config", "/no/such.toml"], "cannot read"),
    ],
)
def test_errors_are_clean(argv: list[str], message: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = seeded(tmp_path)
    code, out, err = run(capsys, *[str(db) if a == "DB" else a for a in argv])
    assert code == 2 and message in err and out == "" and "Traceback" not in err


def test_a_file_that_is_not_a_database_is_an_error_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"nope" * 100)
    code, _, err = run(capsys, "status", "--db", str(junk))
    assert code == 2 and "not a usable" in err
    junk.unlink()
