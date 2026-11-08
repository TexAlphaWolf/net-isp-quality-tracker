"""Reports as Markdown, as one self-contained HTML page (inline SVG, no scripts, no external files), and as
InfluxDB line protocol for anyone who wants Grafana."""

from __future__ import annotations

import html
import math
from collections.abc import Iterator, Sequence

from ispq.analysis import Report, iso, jitter
from ispq.store import Store


def duration(seconds: float) -> str:
    s = round(seconds)
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {sec:02d} s"
    return f"{sec} s"


def _fmt(v: object, unit: str = "") -> str:
    return "-" if v is None else f"{v}{unit}"


def _avail(r: Report) -> str:
    a = r.availability_pct
    return "no data" if a is None else f"{a:.3f} %"


# ---- Markdown ---------------------------------------------------------------------------------


def markdown(r: Report, title: str) -> str:
    out = [f"# Internet quality: {title}", ""]
    if not r.cycles:
        return "\n".join([*out, "No measurements in this period.", ""])
    out += [
        f"- **Availability:** {_avail(r)} ({r.down_cycles} of {r.cycles} cycles down; one cycle every {r.interval:g} s)",
        f"- **Coverage:** {r.coverage_pct:.1f} % of the period has data",
        f"- **Outages:** {len(r.outages)} (total {duration(sum(o.seconds for o in r.outages))}), "
        f"short blips: {len(r.blips)}",
        "",
    ]
    if r.outages:
        out += ["## Outages", "", "| start (UTC) | duration | ended by |", "|---|---|---|"]
        out += [f"| {iso(o.start)} | {duration(o.seconds)} | {o.ended_by} |" for o in r.outages]
        out.append("")
    if r.gaps:
        out += [
            "## Missing data",
            "",
            "The collector was not running, so these periods count neither as up nor down.",
            "",
        ]
        out += ["| from (UTC) | to (UTC) | length |", "|---|---|---|"]
        out += [f"| {iso(a)} | {iso(b)} | {duration(b - a)} |" for a, b in r.gaps]
        out.append("")
    out += [
        "## Targets",
        "",
        "| target | kind | loss | p50 | p95 | p99 | max | jitter | download |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in r.targets.values():
        d = s.summary()
        dl = f"{d['mbps_median']} Mbit/s (min {d['mbps_min']})" if "mbps_median" in d else "-"
        out.append(
            f"| {s.target} | {s.kind} | {s.loss_pct:.2f} % | {_fmt(d.get('rtt_p50'), ' ms')} | {_fmt(d.get('rtt_p95'), ' ms')} | "
            f"{_fmt(d.get('rtt_p99'), ' ms')} | {_fmt(d.get('rtt_max'), ' ms')} | {_fmt(d.get('jitter_mean'), ' ms')} | {dl} |"
        )
    out.append("")
    return "\n".join(out)


# ---- HTML with inline SVG ---------------------------------------------------------------------

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 230, 48, 12, 24, 30


def _floats(daily: Sequence[dict[str, object]], key: str) -> list[float | None]:
    return [float(v) if isinstance(v, int | float) else None for v in (d.get(key) for d in daily)]


def _scale(lo: float, hi: float, a: float, b: float, v: float) -> float:
    return a if hi == lo else a + (v - lo) / (hi - lo) * (b - a)


def _nice_max(v: float) -> float:
    """The smallest 1, 2, 5 or 10 times a power of ten that is at least v: a round top for the axis."""
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * mag:
            return float(m * mag)
    return float(v)


def svg_chart(
    days: Sequence[str], series: Sequence[tuple[str, str, Sequence[float | None]]], unit: str, bars: bool = False
) -> str:
    """series: (label, css class, one value per day or None)."""
    n = len(days)
    if n == 0:
        return ""
    values = [v for _, _, vs in series for v in vs if v is not None]
    top = _nice_max(max(values) if values else 1.0)
    x0, x1, y0, y1 = PAD_L, W - PAD_R, H - PAD_B, PAD_T
    slot = (x1 - x0) / n
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(unit)} per day" class="chart">']
    for i in range(5):
        tick = top * i / 4
        y = _scale(0, top, y0, y1, tick)
        parts.append(f'<line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{tick:g}</text>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{x0 + slot * (i + 0.5):.1f}" y="{H - 10}" text-anchor="middle" class="tick">{html.escape(days[i][5:])}</text>'
        )
    for _label, cls, vs in series:
        if bars:
            for i, v in enumerate(vs):
                if v is None:
                    continue
                h = _scale(0, top, 0, y0 - y1, v)
                parts.append(
                    f'<rect x="{x0 + slot * i + 1:.1f}" y="{y0 - h:.1f}" width="{max(1.0, slot - 2):.1f}" height="{h:.1f}" class="{cls}"/>'
                )
        else:
            pts = [(x0 + slot * (i + 0.5), _scale(0, top, y0, y1, v)) for i, v in enumerate(vs) if v is not None]
            if len(pts) > 1:
                parts.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}" class="line {cls}"/>')
            parts += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" class="dot {cls}"/>' for x, y in pts]
    parts.append(f'<text x="4" y="10" class="tick">{html.escape(unit)}</text></svg>')
    return "".join(parts)


CSS = """
:root{--bg:#fff;--fg:#1c2330;--muted:#5d6878;--card:#f5f7fa;--line:#dde2ea;--a:#2563eb;--b:#d97706;--bad:#c2410c;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#10151d;--fg:#e6ebf2;--muted:#9aa6b6;--card:#18202b;--line:#2a3441;--a:#60a5fa;--b:#fbbf24;--bad:#fb923c;--ok:#4ade80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:780px;margin:0 auto;padding:20px 16px 40px}h1{font-size:1.4rem;margin:.2rem 0 1rem}h2{font-size:1.05rem;margin:1.6rem 0 .5rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:1.3rem}.card span{color:var(--muted);font-size:.85rem}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600}
.num{font-variant-numeric:tabular-nums}.chart{width:100%;height:auto}.grid{stroke:var(--line)}.tick{fill:var(--muted);font-size:10px}
.line{fill:none;stroke-width:2}.p50{stroke:var(--a);fill:var(--a)}.p95{stroke:var(--b);fill:var(--b)}.down{fill:var(--bad)}.dot{stroke:none}
.muted{color:var(--muted)}.legend{font-size:.85rem;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px}
"""


def html_page(r: Report, title: str) -> str:
    e = html.escape
    body = [f"<h1>Internet quality: {e(title)}</h1>"]
    if not r.cycles:
        body.append('<p class="muted">No measurements in this period.</p>')
    else:
        avail = r.availability_pct
        cards = [
            (_avail(r), "availability"),
            (str(len(r.outages)), f"outages ({duration(sum(o.seconds for o in r.outages))})"),
            (str(len(r.blips)), "short blips"),
            (f"{r.coverage_pct:.0f} %", "of the period has data"),
        ]
        body.append(
            '<div class="cards">'
            + "".join(f'<div class="card"><b>{e(a)}</b><span>{e(b)}</span></div>' for a, b in cards)
            + "</div>"
        )
        days = [str(d["day"]) for d in r.daily]
        lat = [
            ("p50", "p50", _floats(r.daily, "rtt_p50")),
            ("p95", "p95", _floats(r.daily, "rtt_p95")),
        ]
        if any(v is not None for _, _, vs in lat for v in vs):
            body.append("<h2>Latency per day (TCP connect, all targets)</h2>")
            body.append(
                '<p class="legend"><i style="background:var(--a)"></i>median<i style="background:var(--b)"></i>95th percentile</p>'
            )
            body.append(svg_chart(days, lat, "ms"))
        down = [("down", "down", [round((v or 0) * r.interval / 60, 1) for v in _floats(r.daily, "down_cycles")])]
        if any(v for _, _, vs in down for v in vs):
            body.append("<h2>Time down per day</h2>")
            body.append(svg_chart(days, down, "minutes", bars=True))
        elif avail is not None:
            body.append('<p class="muted">No downtime in this period.</p>')
        if r.outages:
            body.append("<h2>Outages</h2><table><tr><th>start (UTC)</th><th>duration</th><th>ended by</th></tr>")
            body += [
                f'<tr><td class="num">{e(iso(o.start))}</td><td>{e(duration(o.seconds))}</td><td>{e(o.ended_by)}</td></tr>'
                for o in r.outages
            ]
            body.append("</table>")
        if r.gaps:
            body.append(
                '<h2>Missing data</h2><p class="muted">The collector was not running: not counted as up or down.</p><table>'
            )
            body.append("<tr><th>from (UTC)</th><th>to (UTC)</th><th>length</th></tr>")
            body += [
                f'<tr><td class="num">{e(iso(a))}</td><td class="num">{e(iso(b))}</td><td>{e(duration(b - a))}</td></tr>'
                for a, b in r.gaps
            ]
            body.append("</table>")
        body.append(
            "<h2>Targets</h2><table><tr><th>target</th><th>kind</th><th>loss</th><th>p50</th><th>p95</th><th>p99</th><th>jitter</th><th>download</th></tr>"
        )
        for s in r.targets.values():
            d = s.summary()
            dl = f"{d['mbps_median']} Mbit/s" if "mbps_median" in d else "-"
            cells = [
                s.target,
                s.kind,
                f"{s.loss_pct:.2f} %",
                _fmt(d.get("rtt_p50"), " ms"),
                _fmt(d.get("rtt_p95"), " ms"),
                _fmt(d.get("rtt_p99"), " ms"),
                _fmt(d.get("jitter_mean"), " ms"),
                dl,
            ]
            body.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in cells) + "</tr>")
        body.append("</table>")
        body.append(
            f'<p class="muted">One measurement cycle every {r.interval:g} s; times in UTC. Generated by ispq.</p>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Internet quality {e(title)}</title><style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>"
    )


# ---- InfluxDB line protocol -------------------------------------------------------------------


def _tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ").replace("\n", " ")


def influx_lines(store: Store, start: float, end: float) -> Iterator[str]:
    """One line per round: `ispq,kind=tcp,target=cloudflare sent=5i,ok=5i,rtt_avg=12.1,... <ns>`."""
    for r in store.rows(start, end):
        fields = [f"sent={r.sent}i", f"ok={r.ok}i"]
        if r.rtts:
            fields += [
                f"rtt_avg={sum(r.rtts) / len(r.rtts):.3f}",
                f"rtt_min={min(r.rtts):.3f}",
                f"rtt_max={max(r.rtts):.3f}",
            ]
            j = jitter(r.rtts)
            if j is not None:
                fields.append(f"jitter={j:.3f}")
        if r.mbps is not None:
            fields.append(f"mbps={r.mbps:.3f}")
        yield f"ispq,kind={_tag(r.kind)},target={_tag(r.target)} {','.join(fields)} {int(r.cycle * 1e9)}"
