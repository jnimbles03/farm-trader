#!/usr/bin/env python3
"""
One-shot bootstrap: seed state/price_history.json with ~1 year of daily
closes for corn/soy/wheat via Yahoo Finance's chart API.

Yahoo blocks GH Actions datacenter IPs, so this must be run locally. The
resulting file is committed; `evaluate.py` then just appends to it each
15-min run.

Usage:
    python scripts/bootstrap_history.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT          = Path(__file__).resolve().parent.parent
HISTORY_FILE  = ROOT / "state" / "price_history.json"

# Yahoo symbols for CBOT continuous front-month contracts.
# Returned prices are cents/bu for grains — scale to $/bu to match evaluate.py.
SYMBOLS = {
    "corn":  ("ZC=F", 0.01),
    "soy":   ("ZS=F", 0.01),
    "wheat": ("ZW=F", 0.01),
}

RANGE = "1y"
INTERVAL = "1d"


def fetch(symbol: str) -> list[tuple[str, float]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": INTERVAL, "range": RANGE}
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    out = []
    for ts, px in zip(timestamps, closes):
        if px is None:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append((date, float(px)))
    return out


def main() -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if HISTORY_FILE.exists():
        try:
            existing = json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            existing = {}

    for key, (ysym, scale) in SYMBOLS.items():
        rows = fetch(ysym)
        scaled = [[d, round(p * scale, 4)] for d, p in rows]
        existing[key] = scaled
        print(f"{key}: {len(scaled)} rows  ({scaled[0][0]} → {scaled[-1][0]})")

    HISTORY_FILE.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n"
    )
    print(f"wrote {HISTORY_FILE}")


if __name__ == "__main__":
    main()
