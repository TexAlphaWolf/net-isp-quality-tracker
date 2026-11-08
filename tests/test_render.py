from __future__ import annotations

import re

from helpers import T0, fill, tcp_round
from ispq import render
from ispq.analysis import build_report
from ispq.probes import Round
from ispq.store import Store


def sample_store() -> Store:
    s = Store(":memory:")
    fill(s, ".." + "x" * 4 + "." * 20 + "_" * 6 + "." * 10, interval=60)
    return s


def test_duration() -> None:
    assert render.duration(0) == "0 s" and render.duration(59.6) == "1 min 00 s"
    assert render.duration(125) == "2 min 05 s" and render.duration(3725) == "1 h 02 min"


def test_markdown_report() -> None:
    s = sample_store()
    md = render.markdown(build_report(s, T0, T0 + 3600), "2026-11")
    assert md.startswith("# Internet quality: 2026-11")
    assert "**Availability:** 88.889 %" in md and "**Outages:** 1" in md
    assert "## Outages" in md and "## Missing data" in md and "## Targets" in md
    assert re.search(r"\| a \| tcp \| ", md) and re.search(r"\| b \| tcp \| ", md)
    s.close()


def test_markdown_without_data() -> None:
    s = Store(":memory:")
    assert "No measurements" in render.markdown(build_report(s, T0, T0 + 60), "x")
    assert "No measurements" in render.html_page(build_report(s, T0, T0 + 60), "x")
    s.close()


def test_html_is_self_contained_and_script_free() -> None:
    s = sample_store()
    page = render.html_page(build_report(s, T0, T0 + 3600), "2026-11")
    assert (page.startswith("<!doctype html>") and "<svg" not in page) or "<svg" in page
    assert "<script" not in page.lower() and "http://" not in page and "https://" not in page
    assert "onerror" not in page and "<link" not in page and "<img" not in page
    s.close()


def test_html_escapes_everything_that_came_from_outside() -> None:
    s = Store(":memory:")
    evil = '<script>alert(1)</script>"&'
    s.add_cycle(T0, [Round("tcp", evil, 5, 5, [10.0, 11.0], "")])
    page = render.html_page(build_report(s, T0, T0 + 60), '<b onload="x">')
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert '<b onload="x">' not in page and "&lt;b onload=&quot;x&quot;&gt;" in page
    s.close()


def test_charts_appear_only_when_there_is_something_to_show() -> None:
    s = Store(":memory:")
    fill(s, "." * 10)
    page = render.html_page(build_report(s, T0, T0 + 600), "t")
    assert "Latency per day" in page and "No downtime" in page and "Time down per day" not in page
    s.close()
    page = render.html_page(build_report(sample_store(), T0, T0 + 3600), "t")
    assert "Time down per day" in page and "<rect" in page


def test_svg_chart_edge_cases() -> None:
    assert render.svg_chart([], [], "ms") == ""
    one = render.svg_chart(["2026-11-01"], [("p50", "p50", [12.0])], "ms")
    assert "<circle" in one and "<polyline" not in one  # a single day: a dot, no line
    gaps = render.svg_chart(["a-01", "a-02", "a-03"], [("p", "p50", [1.0, None, 3.0])], "ms")
    assert gaps.count("<circle") == 2
    assert "NaN" not in render.svg_chart(["d-1"], [("p", "p50", [0.0])], "ms")
    for v, top in ((0, 1), (0.7, 1), (1.2, 2), (13, 20), (480, 500), (5000, 5000)):
        assert render._nice_max(v) == top


def test_influx_lines() -> None:
    s = Store(":memory:")
    s.add_cycle(
        T0,
        [
            tcp_round("a b,c=d", 3, rtt=10),
            Round("http", "speed", 1, 1, [30.0], "", 1000, 55.5),
            Round("tcp", "down", 5, 0),
        ],
    )
    lines = list(render.influx_lines(s, T0, T0 + 1))
    assert len(lines) == 3
    by = {ln.split(" ")[0] if "\\ " not in ln else ln.split(",target=")[1].split(" ")[0]: ln for ln in lines}
    assert r"ispq,kind=tcp,target=a\ b\,c\=d sent=5i,ok=3i" in lines[0]
    assert lines[0].endswith(f" {int(T0 * 1e9)}") and "rtt_avg=11.000" in lines[0] and "jitter=1.000" in lines[0]
    http = next(ln for ln in lines if "kind=http" in ln)
    assert "mbps=55.500" in http and "sent=1i,ok=1i,rtt_avg=30.000" in http and "jitter" not in http
    down = next(ln for ln in lines if "target=down" in ln)
    assert down.split(" ")[1] == "sent=5i,ok=0i"  # no latency fields when nothing answered
    assert by
    s.close()
