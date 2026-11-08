"""Render docs/demo.png: the HTML report of a month of SYNTHETIC measurements (a real line is not needed to show the page).

pip install -e ".[dev]" && playwright install chromium
python tools/screenshot.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from ispq import analysis, render
from ispq.store import Store

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.png"
MONTH = "2026-10"


def at(day: int, hh: int, mm: int) -> float:
    return dt.datetime(2026, 10, day, hh, mm, tzinfo=dt.UTC).timestamp()


def build(db: Path) -> None:
    rng = random.Random(11)
    start, end = analysis.month_range(MONTH)
    outages = [(at(6, 14, 5), 7 * 60), (at(14, 3, 30), 3 * 60), (at(21, 20, 10), 25 * 60)]
    gap = (at(17, 8, 0), at(17, 13, 0))  # the collector was off
    targets = [
        ("tcp", "cloudflare", 11.0),
        ("tcp", "google", 14.0),
        ("tcp", "quad9", 19.0),
        ("dns", "dns-cloudflare", 16.0),
    ]
    store = Store(db)
    with store.transaction():
        ts = start
        while ts < end:
            if not gap[0] <= ts < gap[1]:
                down = any(s <= ts < s + d for s, d in outages)
                hour = (ts % 86400) / 3600
                busy = 1.0 + 0.5 * max(0.0, math.sin((hour - 14) / 24 * 2 * math.pi))  # slower in the evening
                batch = []
                for kind, name, base in targets:
                    n = 5 if kind == "tcp" else 3
                    rtts = (
                        []
                        if down
                        else [
                            round(base * busy * rng.lognormvariate(0, 0.25), 3)
                            for _ in range(n)
                            if rng.random() > 0.005
                        ]
                    )
                    batch.append(
                        (ts, kind, name, n, len(rtts), json.dumps(rtts), "timeout" if down else "", None, None)
                    )
                store.db.executemany("INSERT INTO rounds VALUES (?,?,?,?,?,?,?,?,?)", batch)
            ts += 60
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "demo.db"
        build(db)
        lo, hi = analysis.month_range(MONTH)
        store = Store(db)
        page = render.html_page(analysis.build_report(store, lo, hi), f"{MONTH} (synthetic data)")
        store.close()
        html = Path(tmp) / "report.html"
        html.write_text(page, encoding="utf-8")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            shot = browser.new_page(viewport={"width": 820, "height": 900}, device_scale_factor=2)
            shot.goto(html.as_uri())
            shot.screenshot(path=str(OUT), full_page=True)
            browser.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
