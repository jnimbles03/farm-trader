#!/usr/bin/env python3
"""
Freis Farm price + signal evaluator — single-shot.

Runs once, writes its outputs to disk, exits. Designed to be the
payload of a GitHub Actions cron job:

  every 15 min during market hours -> `python evaluate.py`

Outputs (committed back to the repo by the workflow):

  docs/prices.json        served via GitHub Pages; React site fetches this
  docs/signals.json       current model output, for the landing page
  docs/last_run.json      metadata (ran at, market_hours, sms_fired)
  state/alerts_state.json alert de-dupe state, read on next run

Side effects: fires a TextBelt SMS to $ALERT_PHONE on any signal that
transitions WAIT -> HIT, subject to a 24-hour cooldown per signal key.

Environment (set as GitHub Actions secrets):

  TEXTBELT_KEY     paid key from textbelt.com (MVP)
  ALERT_PHONE      E.164-ish, e.g. "+13125551234"
  ALERT_COOLDOWN   seconds between repeat alerts (default 86400)

This script deliberately has no FastAPI / scheduler / SQLite. Scheduling
is the workflow's job; state persistence is git commits.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yfinance as yf


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT         = Path(__file__).resolve().parent
CONTRACTS    = ROOT / "contracts.json"
DOCS_DIR     = ROOT / "docs"
STATE_DIR    = ROOT / "state"
STATE_FILE   = STATE_DIR / "alerts_state.json"
PRICES_FILE  = DOCS_DIR / "prices.json"
SIGNALS_FILE = DOCS_DIR / "signals.json"
LASTRUN_FILE = DOCS_DIR / "last_run.json"

DOCS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEXTBELT_KEY   = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE    = os.environ.get("ALERT_PHONE", "")
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "86400"))  # 24h

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
)
log = logging.getLogger("evaluate")


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

GRAIN_ROOT = {
    "corn":         "ZC",
    "soybean":      "ZS",
    "soybeans":     "ZS",
    "wheat":        "ZW",
    "kc_wheat":     "KE",
    "soybean_meal": "ZM",
    "soybean_oil":  "ZL",
    "oats":         "ZO",
}

# Yahoo quotes grains in cents/bu (ZC=F = 461.75 = $4.6175/bu),
# soy oil in cents/lb, soy meal in $/short ton. Normalize at the edge.
GRAIN_SCALE = {
    "corn":         0.01,
    "soybean":      0.01,
    "soybeans":     0.01,
    "wheat":        0.01,
    "kc_wheat":     0.01,
    "oats":         0.01,
    "soybean_oil":  0.01,
    "soybean_meal": 1.0,
}

MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


def yahoo_symbol(commodity: str,
                 year: int | None = None,
                 month: int | None = None) -> str:
    root = GRAIN_ROOT[commodity.lower()]
    if year is None or month is None:
        return f"{root}=F"
    return f"{root}{MONTH_CODES[month]}{year % 100:02d}.CBT"


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------

_cache: dict[str, float] = {}


def _fetch_raw(symbol: str) -> float:
    t = yf.Ticker(symbol)
    hist = t.history(period="1d", interval="1m")
    if hist.empty:
        hist = t.history(period="5d", interval="1d")
    if hist.empty:
        raise RuntimeError(f"no data for {symbol}")
    return float(hist["Close"].iloc[-1])


def get_price(commodity: str,
              year: int | None = None,
              month: int | None = None) -> float | None:
    """Dollar-denominated last price, None on failure."""
    scale = GRAIN_SCALE.get(commodity.lower(), 1.0)
    sym = yahoo_symbol(commodity, year, month)
    if sym in _cache:
        return _cache[sym] * scale
    try:
        raw = _fetch_raw(sym)
        _cache[sym] = raw
        return raw * scale
    except Exception as e:
        log.warning("fetch failed for %s: %s", sym, e)
        if year is not None:
            # Specific-month symbols on Yahoo are flaky — fall back to
            # continuous front-month so the row still renders.
            try:
                fallback = yahoo_symbol(commodity)
                raw = _cache.get(fallback) or _fetch_raw(fallback)
                _cache[fallback] = raw
                return raw * scale
            except Exception as e2:
                log.warning("continuous fallback failed: %s", e2)
        return None


# ---------------------------------------------------------------------------
# Contracts + model
# ---------------------------------------------------------------------------

@dataclass
class Contract:
    contract_id: str
    commodity: str
    contract_type: str
    futures_year: int
    futures_month: int
    futures_price: float | None
    basis_cost: float | None
    quantity: float


@dataclass
class Signal:
    signal_key: str
    contract_id: str
    commodity: str
    futures_year: int
    futures_month: int
    action: str              # SELL | BUY_BACK
    target_price: float
    note: str


def load_contracts() -> list[Contract]:
    with CONTRACTS.open() as f:
        data = json.load(f)
    return [Contract(**row) for row in data]


def model(contracts: list[Contract]) -> list[Signal]:
    """
    Toy schedule: every open HTA gets a SELL at locked + $0.20.
    Replace with real schedule logic when it's defined.
    """
    out: list[Signal] = []
    for c in contracts:
        if c.contract_type == "HTA" and c.futures_price is not None:
            tgt = round(c.futures_price + 0.20, 4)
            key = f"{c.commodity}|{c.futures_year}-{c.futures_month:02d}|SELL|{tgt:.4f}"
            out.append(Signal(
                signal_key=key,
                contract_id=c.contract_id,
                commodity=c.commodity,
                futures_year=c.futures_year,
                futures_month=c.futures_month,
                action="SELL",
                target_price=tgt,
                note=f"locked + $0.20 on {c.contract_id}",
            ))
    return out


def signal_hit(action: str, live: float, target: float) -> bool:
    if action == "SELL":     return live >= target
    if action == "BUY_BACK": return live <= target
    return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("state file is corrupt, resetting: %s", e)
        return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------

def send_sms(message: str) -> bool:
    """Return True iff the SMS was actually accepted by TextBelt."""
    if not TEXTBELT_KEY or not ALERT_PHONE:
        log.warning("SMS skipped — TEXTBELT_KEY or ALERT_PHONE not set")
        return False
    try:
        r = httpx.post(
            "https://textbelt.com/text",
            data={"phone": ALERT_PHONE, "message": message, "key": TEXTBELT_KEY},
            timeout=15.0,
        )
        body = r.json()
        log.info("textbelt: %s", body)
        return bool(body.get("success"))
    except Exception as e:
        log.exception("textbelt call failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_prices_snapshot() -> dict[str, Any]:
    """
    Response shape matches what freis-farm-v5.jsx fetchGrainPrices() expects.
    Fields that fail to fetch stay null; the React site's existing null-guard
    keeps it from crashing.
    """
    corn_front  = get_price("corn")
    soy_front   = get_price("soybean")
    wheat_front = get_price("wheat")
    corn_dec    = get_price("corn",    2026, 12)
    soy_nov     = get_price("soybean", 2026, 11)
    wheat_jul   = get_price("wheat",   2026,  7)

    def r(v): return round(v, 4) if v is not None else None

    return {
        "corn":          r(corn_front),
        "soy":           r(soy_front),
        "wheat":         r(wheat_front),
        "corn_dec":      r(corn_dec),
        "soy_nov":       r(soy_nov),
        "wheat_jul":     r(wheat_jul),
        "corn_basis_il": None,   # Yahoo doesn't carry IL cash basis
        "soy_basis_il":  None,
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source":        "yahoo finance (~10 min delayed)",
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }


def evaluate_signals(state: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """
    Walk the signals, compare live vs target, mutate `state` in place,
    fire SMS on fresh hits. Returns (rows, sms_fired_count).
    """
    contracts = load_contracts()
    signals = model(contracts)

    fired = 0
    rows: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for s in signals:
        live = get_price(s.commodity, s.futures_year, s.futures_month)
        if live is None:
            rows.append({**asdict(s), "live": None, "status": "NO_DATA"})
            continue

        hit = signal_hit(s.action, live, s.target_price)
        new_status = "HIT" if hit else "WAIT"

        prior = state.get(s.signal_key) or {}
        prior_status = prior.get("status", "WAIT")
        last_fired = prior.get("last_fired")

        should_fire = False
        if hit and prior_status != "HIT":
            if last_fired:
                elapsed = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last_fired)).total_seconds()
                should_fire = elapsed >= ALERT_COOLDOWN
            else:
                should_fire = True

        if should_fire:
            msg = (f"FREIS FARM {s.action} target hit — "
                   f"{s.commodity} {s.futures_year}-{s.futures_month:02d} "
                   f"live {live:.4f} vs target {s.target_price:.4f} "
                   f"({s.note})")
            if send_sms(msg):
                fired += 1
                last_fired = now_iso
            else:
                # Don't set last_fired on failure — we want the next run
                # to try again rather than silently sitting in cooldown.
                log.warning("SMS did NOT land for %s — will retry next run",
                            s.signal_key)

        state[s.signal_key] = {
            "status":       new_status,
            "last_fired":   last_fired,
            "last_price":   round(live, 4),
            "last_updated": now_iso,
        }

        rows.append({
            **asdict(s),
            "live":         round(live, 4),
            "status":       new_status,
            "prior_status": prior_status,
            "last_fired":   last_fired,
        })

    return rows, fired


def main() -> int:
    log.info("evaluate starting")
    state = load_state()

    prices = build_prices_snapshot()
    PRICES_FILE.write_text(json.dumps(prices, indent=2) + "\n")
    log.info("wrote %s", PRICES_FILE)

    rows, fired = evaluate_signals(state)
    signals_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signals":      rows,
    }
    SIGNALS_FILE.write_text(json.dumps(signals_payload, indent=2) + "\n")
    log.info("wrote %s — %s signals, %s SMS fired", SIGNALS_FILE, len(rows), fired)

    save_state(state)
    log.info("wrote %s", STATE_FILE)

    LASTRUN_FILE.write_text(json.dumps({
        "ran_at":      datetime.now(timezone.utc).isoformat(),
        "signal_count": len(rows),
        "sms_fired":    fired,
        "sms_ready":    bool(TEXTBELT_KEY and ALERT_PHONE),
    }, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
