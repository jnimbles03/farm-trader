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


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT         = Path(__file__).resolve().parent
CONTRACTS    = ROOT / "contracts.json"
DOCS_DIR     = ROOT / "docs"
STATE_DIR    = ROOT / "state"
STATE_FILE   = STATE_DIR / "alerts_state.json"
HISTORY_FILE = STATE_DIR / "price_history.json"
PRICES_FILE  = DOCS_DIR / "prices.json"
SIGNALS_FILE = DOCS_DIR / "signals.json"
LASTRUN_FILE = DOCS_DIR / "last_run.json"

DOCS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEXTBELT_KEY         = os.environ.get("TEXTBELT_KEY", "")
TWILIO_ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM          = os.environ.get("TWILIO_FROM", "")     # Twilio-owned phone number
ALERT_PHONE          = os.environ.get("ALERT_PHONE", "")
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
    "corn":         "zc",
    "soybean":      "zs",
    "soybeans":     "zs",
    "wheat":        "zw",
    "kc_wheat":     "ke",
    "soybean_meal": "zm",
    "soybean_oil":  "zl",
    "oats":         "zo",
}

# Stooq quotes grains in cents/bu (ZC.C = 456.5 = $4.565/bu),
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


def stooq_symbol(commodity: str,
                 year: int | None = None,
                 month: int | None = None) -> str:
    # Stooq's free CSV feed only carries continuous front-month for CBOT
    # grains — specific contract months return N/D. year/month are accepted
    # for API parity; callers get continuous regardless.
    return f"{GRAIN_ROOT[commodity.lower()]}.c"


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------

_cache: dict[str, float] = {}

# Stooq is happy to serve CSV from datacenter IPs (GitHub Actions, etc.),
# unlike Yahoo which blocks yfinance and the chart API with 429s.
# Row format: Symbol,Date,Time,Open,High,Low,Close,Volume
#
# Returns (close, date_str). Stooq's free daily-history endpoint now requires
# an API key, so we only use this light quote endpoint and accumulate our
# own history in state/price_history.json across runs.
def _fetch_raw(symbol: str) -> tuple[float, str]:
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    with httpx.Client(timeout=10, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"no rows for {symbol}")
    cols = lines[1].split(",")
    date  = cols[1] if len(cols) > 1 else ""
    close = cols[6] if len(cols) > 6 else "N/D"
    if close in ("N/D", ""):
        raise RuntimeError(f"no close for {symbol}")
    return float(close), date


# ---------------------------------------------------------------------------
# Self-accumulating daily history
# ---------------------------------------------------------------------------
#
# Stooq's /q/d/l/ (full daily history) now gates behind an API key, so we
# build our own rolling file at state/price_history.json. Each GitHub
# Actions run appends the latest close if the date is new, trims to the
# last ~260 trading days (~1 year), and commits the file back. First
# few runs won't have day_chg/52w yet — dashboard degrades gracefully.

HISTORY_MAX = 260  # trading days retained per symbol

def load_history() -> dict[str, list[list[Any]]]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("price_history is corrupt, resetting: %s", e)
        return {}


def save_history(hist: dict[str, list[list[Any]]]) -> None:
    HISTORY_FILE.write_text(json.dumps(hist, indent=2, sort_keys=True) + "\n")


def _append_history(hist: dict[str, list[list[Any]]],
                    key: str, date: str, close_dollar: float) -> None:
    """Upsert today's close under `key`, keep the series sorted + trimmed.

    Uses a dict to dedupe by date (later writes for the same trade date win —
    useful when an intraday run later gets corrected to an official close).
    """
    if not date or close_dollar is None:
        return
    series = hist.get(key) or []
    by_date = {row[0]: row[1] for row in series}
    by_date[date] = round(close_dollar, 4)
    merged = sorted(by_date.items())
    if len(merged) > HISTORY_MAX:
        merged = merged[-HISTORY_MAX:]
    hist[key] = [[d, p] for d, p in merged]


def _price_detail(key: str, hist: dict[str, list[list[Any]]],
                  live: float | None = None, as_of: str | None = None) -> dict[str, Any]:
    """
    Derive day-over-day change + rolling range from the accumulated history.
    If `live` + `as_of` are provided, they take precedence as "latest close" —
    this keeps the top-level price and the detail block in lockstep even when
    the seeded bootstrap has a newer row than Stooq's end-of-day feed.
    """
    series = hist.get(key) or []
    if not series and live is None:
        return {}

    # Resolve latest close
    if live is not None:
        last = live
        last_date = as_of or (series[-1][0] if series else "")
    else:
        last_date, last = series[-1]

    # Find the most recent entry in history strictly before last_date
    prev = None
    for d, p in reversed(series):
        if d < last_date:
            prev = p
            break

    chg     = (last - prev) if prev is not None else None
    chg_pct = (chg / prev * 100) if prev else None
    highs = [row[1] for row in series] + ([last] if live is not None else [])
    return {
        "prev_close":  round(prev, 4)    if prev is not None else None,
        "day_chg":     round(chg, 4)     if chg is not None else None,
        "day_chg_pct": round(chg_pct, 2) if chg_pct is not None else None,
        "high_range":  round(max(highs), 4),
        "low_range":   round(min(highs), 4),
        "range_days":  len(series),
        "as_of":       last_date,
    }


def get_price(commodity: str,
              year: int | None = None,
              month: int | None = None) -> float | None:
    """Dollar-denominated last price, None on failure."""
    scale = GRAIN_SCALE.get(commodity.lower(), 1.0)
    sym = stooq_symbol(commodity, year, month)
    if sym in _cache:
        return _cache[sym] * scale
    try:
        raw, _date = _fetch_raw(sym)
        _cache[sym] = raw
        return raw * scale
    except Exception as e:
        log.warning("fetch failed for %s: %s", sym, e)
        if year is not None:
            # Specific-month symbols on Yahoo are flaky — fall back to
            # continuous front-month so the row still renders.
            try:
                fallback = stooq_symbol(commodity)
                if fallback in _cache:
                    raw = _cache[fallback]
                else:
                    raw, _ = _fetch_raw(fallback)
                    _cache[fallback] = raw
                return raw * scale
            except Exception as e2:
                log.warning("continuous fallback failed: %s", e2)
        return None


def get_price_with_date(commodity: str) -> tuple[float | None, str | None]:
    """Dollar-denominated last close + its trade date, for history recording."""
    scale = GRAIN_SCALE.get(commodity.lower(), 1.0)
    sym = stooq_symbol(commodity)
    try:
        raw, date = _fetch_raw(sym)
        _cache[sym] = raw
        return raw * scale, date
    except Exception as e:
        log.warning("dated fetch failed for %s: %s", sym, e)
        return None, None


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
    """Dispatch to the first configured SMS provider. True iff accepted.

    Preference order:
      1. Twilio (TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_FROM)
      2. TextBelt (TEXTBELT_KEY)
    Both require ALERT_PHONE set.
    """
    if not ALERT_PHONE:
        log.warning("SMS skipped — ALERT_PHONE not set")
        return False
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM:
        return _send_sms_twilio(message)
    if TEXTBELT_KEY:
        return _send_sms_textbelt(message)
    log.warning("SMS skipped — no SMS provider secrets configured")
    return False


def _send_sms_twilio(message: str) -> bool:
    """POST to Twilio's REST API. Returns True on HTTP 2xx with no error_code.

    Twilio responds 201 with JSON like
      {"sid":"SMxxx","status":"queued","error_code":null,"error_message":null}
    on accept; we treat any non-2xx OR any non-null error_code as failure.
    """
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        r = httpx.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_FROM,
                "To":   ALERT_PHONE,
                "Body": message,
            },
            timeout=15.0,
        )
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text}
        # Successful responses use "status" + "error_code"/"error_message".
        # 4xx/5xx errors use "code" + "message" + "more_info".
        err_code = body.get("error_code") or body.get("code")
        err_msg  = body.get("error_message") or body.get("message")
        log.info("twilio: http=%d sid=%s status=%s err_code=%s err_msg=%s",
                 r.status_code,
                 body.get("sid"),
                 body.get("status"),
                 err_code,
                 err_msg)
        return r.status_code < 300 and not err_code
    except Exception as e:
        log.exception("twilio call failed: %s", e)
        return False


def _send_sms_textbelt(message: str) -> bool:
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

    Side effect: appends today's close for corn/soy/wheat to
    state/price_history.json, which backs the day-change / range detail.
    """
    corn_front_date  = get_price_with_date("corn")
    soy_front_date   = get_price_with_date("soybean")
    wheat_front_date = get_price_with_date("wheat")
    corn_front,  corn_date  = corn_front_date
    soy_front,   soy_date   = soy_front_date
    wheat_front, wheat_date = wheat_front_date

    corn_dec    = get_price("corn",    2026, 12)
    soy_nov     = get_price("soybean", 2026, 11)
    wheat_jul   = get_price("wheat",   2026,  7)

    # Accumulate history and persist
    hist = load_history()
    if corn_front  is not None: _append_history(hist, "corn",  corn_date,  corn_front)
    if soy_front   is not None: _append_history(hist, "soy",   soy_date,   soy_front)
    if wheat_front is not None: _append_history(hist, "wheat", wheat_date, wheat_front)
    save_history(hist)

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
        "detail": {
            "corn":  _price_detail("corn",  hist, corn_front,  corn_date),
            "soy":   _price_detail("soy",   hist, soy_front,   soy_date),
            "wheat": _price_detail("wheat", hist, wheat_front, wheat_date),
        },
        "date":          datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source":        "stooq (end-of-day close)",
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
    # --test-sms: smoke-test the SMS pipeline by firing one message. Writes
    # no other outputs. Polls Twilio for final status so the job tells the
    # truth about carrier delivery, not just queueing. Exits 0 on delivered.
    # --force-textbelt: skip Twilio even if configured, use TextBelt path.
    if "--test-sms" in sys.argv:
        log.info("evaluate --test-sms: firing test message")
        msg = (f"FREIS FARM test SMS — "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
               f"if you got this, the pipeline works.")
        force_textbelt = "--force-textbelt" in sys.argv
        # Capture the Twilio sid so we can poll; the shared send_sms() only
        # returns bool. Call Twilio directly here when configured.
        import time
        final_status: str | None = None
        if (not force_textbelt
            and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM and ALERT_PHONE):
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
            r = httpx.post(
                url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"From": TWILIO_FROM, "To": ALERT_PHONE, "Body": msg},
                timeout=15.0,
            )
            try:
                body = r.json()
            except Exception:
                body = {}
            sid = body.get("sid")
            err_code = body.get("error_code") or body.get("code")
            err_msg  = body.get("error_message") or body.get("message")
            log.info("twilio create: http=%d sid=%s status=%s err_code=%s err_msg=%s",
                     r.status_code, sid, body.get("status"), err_code, err_msg)
            if not sid or r.status_code >= 300 or err_code:
                return 1
            # Poll up to 30s for a terminal status
            poll_url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages/{sid}.json"
            for i in range(10):
                time.sleep(3)
                p = httpx.get(poll_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15.0)
                try:
                    pbody = p.json()
                except Exception:
                    pbody = {}
                status = pbody.get("status")
                pec    = pbody.get("error_code")
                pem    = pbody.get("error_message")
                log.info("twilio poll[%d]: status=%s err_code=%s err_msg=%s", i + 1, status, pec, pem)
                if status in ("delivered", "undelivered", "failed", "sent"):
                    final_status = status
                    break
            log.info("test SMS final status: %s", final_status or "unknown (still queued/sending)")
            return 0 if final_status == "delivered" else 1
        # Fallback: TextBelt path — only tells us whether it was accepted
        ok = send_sms(msg)
        log.info("test SMS %s", "ACCEPTED" if ok else "FAILED")
        return 0 if ok else 1

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
        "sms_ready":    bool(ALERT_PHONE and (
                            (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM)
                            or TEXTBELT_KEY
                        )),
    }, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
