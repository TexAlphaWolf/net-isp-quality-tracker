"""The numbers quoted in the docs must be the ones in docs/report.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "docs" / "report.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
EVAL = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")


def test_report_is_a_full_run() -> None:
    assert REPORT["quick"] is False
    assert REPORT["analysis"]["simulated_months"] == 200


def test_outage_detection_numbers() -> None:
    od = REPORT["analysis"]["outage_detection"]
    assert od["recall_pct"] == 100.0 and od["false_outages"] == 0
    assert f"{od['detected_once']} of {od['injected_clean_outages']}" in README
    assert str(od["injected_clean_outages"]) in EVAL


def test_missing_data_numbers() -> None:
    g = REPORT["analysis"]["outages_interrupted_by_missing_data"]
    assert g["ispq_outages_that_span_missing_data"] == 0
    assert str(g["naive_outages_that_span_missing_data"]) in README and str(g["injected"]) in README
    av = REPORT["analysis"]["availability_error_points"]
    assert f"{av['ispq_mean']:.4f}" in README and f"{av['naive_missing_counts_as_down_mean']:.4f}" in README
    assert f"{av['ispq_mean']:.4f}" in EVAL


def test_percentile_numbers() -> None:
    p = REPORT["analysis"]["percentiles"]
    for s in (f"{p['error_all_samples_pct']:+.2f} %", f"{p['error_round_averages_pct']:+.2f} %"):
        assert s in README and s in EVAL


def test_probe_tables() -> None:
    for x in REPORT["probes"]["dns_latency"]:
        assert f"| {x['injected_ms']} ms | {x['median_measured_ms']:.2f} ms |" in EVAL
    for x in REPORT["probes"]["dns_loss"]:
        assert f"| {x['dropped_by_server']} of {x['queries']} | {x['reported_lost']} |" in EVAL
        assert x["dropped_by_server"] == x["reported_lost"]  # the probe loses nothing the server did not drop
    for x in REPORT["probes"]["http_bandwidth"]:
        assert f"{x['error_pct']:+.2f} %" in EVAL
        assert abs(x["error_pct"]) < 1.5 or "1.5" not in README  # README says "within 1.5 %"
        assert abs(x["error_pct"]) <= 1.5


def test_cost_numbers() -> None:
    c = REPORT["cost"]
    assert f"{c['database']['mb_per_month']} MB" in README and f"{c['report']['one_month_seconds']:.2f} s" in README
    assert f"{c['report']['whole_database_seconds']:.2f} s" in EVAL


def test_the_probe_does_not_lose_connections() -> None:
    t = REPORT["probes"]["tcp_loopback"]
    assert t["ok"] == t["attempts"] and t["closed_port_ok"] == 0
