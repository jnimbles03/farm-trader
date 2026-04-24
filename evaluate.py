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
  ALERT_PHONE      one or more E.164-ish numbers, comma-separated, e.g.
                   "+13125551234,+13125559999" — each costs 1 TextBelt credit
  ALERT_COOLDOWN   seconds between repeat alerts (default 86400)

This script deliberately has no FastAPI / scheduler / SQLite. Scheduling
is the workflow's job; state persistence is git commits.
"""

from __future__ import annotations

import hashlib
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

ROOT               = Path(__file__).resolve().parent
CONTRACTS          = ROOT / "contracts.json"
LEDGER             = ROOT / "sales_ledger.json"
DOCS_DIR           = ROOT / "docs"
STATE_DIR          = ROOT / "state"
STATE_FILE         = STATE_DIR / "alerts_state.json"
HISTORY_FILE       = STATE_DIR / "price_history.json"
CONFIRMATIONS_FILE = STATE_DIR / "confirmations.json"
PRICES_FILE        = DOCS_DIR / "prices.json"
SIGNALS_FILE       = DOCS_DIR / "signals.json"
POSITIONS_FILE     = DOCS_DIR / "positions.json"
LEDGER_FILE        = DOCS_DIR / "ledger.json"
LASTRUN_FILE       = DOCS_DIR / "last_run.json"
PUBLIC_CONF_FILE   = DOCS_DIR / "confirmations.json"

DOCS_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEXTBELT_KEY      = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE       = os.environ.get("ALERT_PHONE", "")
ALERT_COOLDOWN    = int(os.environ.get("ALERT_COOLDOWN", "86400"))  # 24h
# Optional — set this to the Cloudflare Worker URL so recipients can
# reply "Y <code>" / "N <code>" and the worker fans the reply into
# state/confirmations.json via a repository_dispatch event. Leave blank
# to disable the confirmation flow entirely; alerts still go out,
# they just won't carry a reply code.
REPLY_WEBHOOK_URL = os.environ.get("REPLY_WEBHOOK_URL", "")

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
    # CME feeder cattle futures — quoted in ¢/lb on the exchange.
    # Stooq continuous symbol: gf.c. Used by the Cattle tab on the
    # dashboard to show the selling-context for calf sales.
    "feeder_cattle": "gf",
}

# For SMS readability — "Dec '26" beats "2026-12" at 6am.
_MONTH_ABBR = ["",
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

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
    # Feeder cattle: Stooq returns cents/lb; standard presentation in
    # ag media is $/cwt (× 100 lbs). cents/lb × 1.0 = cents/lb; to get
    # $/cwt we'd need × 100 / 100 = identity — so scale stays 1.0 and
    # the dashboard treats the raw value as $/cwt. If Stooq ever returns
    # $/lb instead of cents/lb for this symbol the caller should revisit.
    "feeder_cattle": 1.0,
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
    note: str = ""                   # free-text context shown on dashboards


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

def _recipients() -> list[str]:
    """ALERT_PHONE may be a single number or a comma-separated list.

    We split so the workflow secret can hold multiple recipients without
    introducing ALERT_PHONE_2 / ALERT_PHONE_3 secrets. Whitespace and empty
    entries are trimmed. TextBelt bills 1 credit per number, so sending
    to N numbers deducts N credits per alert.
    """
    return [p.strip() for p in ALERT_PHONE.split(",") if p.strip()]


def send_sms(message: str, reply_webhook_url: str = "") -> bool:
    """Dispatch via TextBelt to every recipient in ALERT_PHONE.

    Returns True iff *every* recipient was accepted. A partial failure
    (one of two numbers accepted) still returns False so the caller
    knows to retry — TextBelt charges per accepted recipient so a retry
    will only cost credits for the previously-failed numbers.

    If `reply_webhook_url` is provided, TextBelt will POST inbound replies
    to it. We use that to fan confirmations into the collect-reply flow.
    """
    recipients = _recipients()
    if not recipients:
        log.warning("SMS skipped — ALERT_PHONE not set")
        return False
    if not TEXTBELT_KEY:
        log.warning("SMS skipped — TEXTBELT_KEY not set")
        return False
    all_ok = True
    for phone in recipients:
        if not _send_sms_textbelt(message, phone, reply_webhook_url):
            all_ok = False
    return all_ok


def _send_sms_textbelt(message: str, phone: str,
                       reply_webhook_url: str = "") -> bool:
    try:
        data = {"phone": phone, "message": message, "key": TEXTBELT_KEY}
        if reply_webhook_url:
            data["replyWebhookUrl"] = reply_webhook_url
        r = httpx.post(
            "https://textbelt.com/text",
            data=data,
            timeout=15.0,
        )
        body = r.json()
        log.info("textbelt[%s]: %s", phone, body)
        return bool(body.get("success"))
    except Exception as e:
        log.exception("textbelt call failed for %s: %s", phone, e)
        return False


# ---------------------------------------------------------------------------
# Confirmations (outbound half — collect_reply.py handles inbound)
# ---------------------------------------------------------------------------

def _short_id(signal_key: str, when: datetime) -> str:
    """Stable 6-char hex ID for one signal firing.

    Combines signal_key + the wall-clock timestamp so a re-fire of the
    same signal (post-cooldown) gets a distinct ID. Truncating sha256 to
    6 hex chars (24 bits) collides ~1 in 16M — fine for tens of alerts.
    """
    payload = f"{signal_key}|{when.isoformat()}".encode()
    return hashlib.sha256(payload).hexdigest()[:6]


def _load_confirmations() -> dict[str, Any]:
    if not CONFIRMATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMATIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("confirmations.json corrupt, resetting: %s", e)
        return {}


def _save_confirmations(data: dict[str, Any]) -> None:
    CONFIRMATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    # Sanitized public copy — phone numbers stripped, just vote tallies.
    # The dashboard can fetch docs/confirmations.json to show status dots.
    sanitized: dict[str, dict[str, Any]] = {}
    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        recipients = entry.get("recipients", {})
        total = len(recipients)
        yes   = sum(1 for r in recipients.values() if r.get("vote") == "Y")
        no    = sum(1 for r in recipients.values() if r.get("vote") == "N")
        sanitized[sid] = {
            "signal_key": entry.get("signal_key"),
            "sent_at":    entry.get("sent_at"),
            "status":     entry.get("status"),
            "total":      total,
            "yes":        yes,
            "no":         no,
        }
    PUBLIC_CONF_FILE.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True) + "\n"
    )


def _record_outbound(sid: str, signal_key: str, message: str,
                     recipients: list[str]) -> None:
    """Stamp one outbound alert into confirmations.json as `pending`."""
    data = _load_confirmations()
    data[sid] = {
        "signal_key": signal_key,
        "sent_at":    datetime.now(timezone.utc).isoformat(),
        "message":    message,
        "status":     "pending",
        "recipients": {p: {"vote": None} for p in recipients},
    }
    _save_confirmations(data)


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

    # Feeder cattle — CME front-month, used by the Cattle tab on the
    # dashboard for calf-sale timing context. Fails quietly if Stooq
    # doesn't carry the symbol; the tile degrades gracefully in that case.
    fc_front, fc_date = get_price_with_date("feeder_cattle")

    # Accumulate history and persist
    hist = load_history()
    if corn_front  is not None: _append_history(hist, "corn",  corn_date,  corn_front)
    if soy_front   is not None: _append_history(hist, "soy",   soy_date,   soy_front)
    if wheat_front is not None: _append_history(hist, "wheat", wheat_date, wheat_front)
    if fc_front    is not None: _append_history(hist, "feeder_cattle", fc_date, fc_front)
    save_history(hist)

    def r(v): return round(v, 4) if v is not None else None

    return {
        "corn":           r(corn_front),
        "soy":            r(soy_front),
        "wheat":          r(wheat_front),
        "corn_dec":       r(corn_dec),
        "soy_nov":        r(soy_nov),
        "wheat_jul":      r(wheat_jul),
        "corn_basis_il":  None,   # Yahoo doesn't carry IL cash basis
        "soy_basis_il":   None,
        "feeder_cattle":  r(fc_front),    # $/cwt — CME front-month
        "detail": {
            "corn":          _price_detail("corn",          hist, corn_front,  corn_date),
            "soy":           _price_detail("soy",           hist, soy_front,   soy_date),
            "wheat":         _price_detail("wheat",         hist, wheat_front, wheat_date),
            "feeder_cattle": _price_detail("feeder_cattle", hist, fc_front,    fc_date),
        },
        "date":           datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source":         "stooq (end-of-day close)",
        "generated_at":   datetime.now(timezone.utc).isoformat(),
    }


def build_positions_snapshot() -> dict[str, Any]:
    """
    Open-position view over contracts.json. Pulls a live price for each
    contract's futures month, computes MTM where the locked futures_price
    is known (HTA / CASH), and leaves mtm null for unpriced commitments
    (APP, INVENTORY, BASIS). Shape is dashboard-friendly — the React site
    can read this directly without re-running the model.
    """
    contracts = load_contracts()
    out: list[dict[str, Any]] = []
    totals = {"corn": 0.0, "soybean": 0.0, "wheat": 0.0}

    for c in contracts:
        live = get_price(c.commodity, c.futures_year, c.futures_month)
        mtm_per_bu = None
        total_mtm = None
        if live is not None and c.futures_price is not None:
            mtm_per_bu = round(live - c.futures_price, 4)
            total_mtm = round(mtm_per_bu * c.quantity, 2)

        out.append({
            "contract_id":   c.contract_id,
            "commodity":     c.commodity,
            "contract_type": c.contract_type,
            "futures_year":  c.futures_year,
            "futures_month": c.futures_month,
            "futures_price": c.futures_price,
            "basis_cost":    c.basis_cost,
            "quantity":      c.quantity,
            "note":          c.note,
            "live":          round(live, 4) if live is not None else None,
            "mtm_per_bu":    mtm_per_bu,
            "total_mtm":     total_mtm,
        })

        key = "soybean" if c.commodity in ("soybean", "soybeans") else c.commodity
        if key in totals:
            totals[key] += float(c.quantity)

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "positions":     out,
        "open_bushels":  {k: round(v) for k, v in totals.items()},
    }


def build_ledger_snapshot() -> dict[str, Any]:
    """
    Pass-through of sales_ledger.json with lightweight derived totals:
    YTD bushels sold per commodity and total cash receipts. Missing
    file returns an empty-but-valid structure so the dashboard doesn't
    have to special-case first-run.
    """
    if not LEDGER.exists():
        return {
            "generated_at":   datetime.now(timezone.utc).isoformat(),
            "trades":         [],
            "cash_receipts":  [],
            "bushels_sold":   {},
            "cash_total":     0.0,
        }

    raw = json.loads(LEDGER.read_text())
    trades = raw.get("trades", [])
    cash   = raw.get("cash_receipts", [])

    sold: dict[str, float] = {}
    for t in trades:
        c = t.get("commodity", "")
        key = "soybean" if c in ("soybean", "soybeans") else c
        sold[key] = sold.get(key, 0.0) + float(t.get("quantity", 0))

    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "trades":         trades,
        "cash_receipts":  cash,
        "bushels_sold":   {k: round(v) for k, v in sold.items()},
        "cash_total":     round(sum(float(r.get("amount", 0)) for r in cash), 2),
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
            now = datetime.now(timezone.utc)
            recipients = _recipients()
            # Human-readable SMS body: lead with the action, then the
            # contract in plain English ("Corn Dec '26"), then the two
            # prices that explain WHY this fired. Note (e.g. "locked +
            # $0.20 on ABC") is stored in state but not shown on SMS —
            # recipients have their own context for what the target is.
            month_abbr = _MONTH_ABBR[s.futures_month]
            yr2 = f"'{s.futures_year % 100:02d}"
            verb = "SELL" if s.action == "SELL" else "BUY BACK"
            base_msg = (f"FREIS FARM {verb} alert: "
                        f"{s.commodity.capitalize()} {month_abbr} {yr2} is at "
                        f"${live:.2f} (target ${s.target_price:.2f})")

            # If the webhook is configured, record the outbound so the
            # inbound collector can match a plain Y/N reply against the
            # most recent pending for that phone. Short_id is kept in
            # state for dashboard/debugging but no longer sent to the
            # recipient — just say "Reply Y / N".
            sid: str | None = None
            if REPLY_WEBHOOK_URL and recipients:
                sid = _short_id(s.signal_key, now)
                msg = base_msg + ", Reply Y to confirm, N to veto."
                _record_outbound(sid, s.signal_key, msg, recipients)
            else:
                msg = base_msg + "."

            if send_sms(msg, reply_webhook_url=REPLY_WEBHOOK_URL):
                fired += 1
                last_fired = now_iso
            else:
                # Don't set last_fired on failure — we want the next run
                # to try again rather than silently sitting in cooldown.
                log.warning("SMS did NOT land for %s — will retry next run",
                            s.signal_key)
                # Drop the outbound record too so a retry can re-mint a
                # fresh short_id rather than leaving a pending ghost.
                if sid is not None:
                    data = _load_confirmations()
                    data.pop(sid, None)
                    _save_confirmations(data)

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
    # --test-sms: smoke-test the SMS pipeline by firing one TextBelt message
    # to every recipient in ALERT_PHONE. Writes no other outputs. Exits 0 iff
    # every recipient was accepted.
    if "--test-sms" in sys.argv:
        log.info("evaluate --test-sms: firing test message")
        msg = (f"FREIS FARM test SMS — "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
               f"if you got this, the pipeline works.")
        recipients = _recipients()
        if not (TEXTBELT_KEY and recipients):
            log.warning("test SMS skipped — TEXTBELT_KEY or ALERT_PHONE missing")
            return 1
        log.info("test SMS recipients: %s", ", ".join(recipients))
        ok = send_sms(msg)
        log.info("test SMS via textbelt: %s", "ALL ACCEPTED" if ok else "PARTIAL/FAIL")
        return 0 if ok else 1

    # --test-confirmation: exercise the full Y/N reply flow. Fires a fake
    # "signal" SMS with a real confirmation code, records the outbound so
    # the inbound collector can match the reply, and commits via the
    # workflow afterward. Recipients reply `Y <code>` → collect-reply.yml
    # lights up → state/confirmations.json updates → group follow-up SMS.
    if "--test-confirmation" in sys.argv:
        log.info("evaluate --test-confirmation: firing fake signal")
        if not REPLY_WEBHOOK_URL:
            log.error("REPLY_WEBHOOK_URL not set — the reply flow can't work")
            return 1
        recipients = _recipients()
        if not (TEXTBELT_KEY and recipients):
            log.warning("test-confirmation skipped — TEXTBELT_KEY or ALERT_PHONE missing")
            return 1
        now = datetime.now(timezone.utc)
        fake_key = f"test|{now.strftime('%Y-%m-%dT%H-%M-%S')}|CONFIRM"
        sid = _short_id(fake_key, now)
        msg = ("FREIS FARM confirmation test — this is a drill, not a "
               "real signal. Reply Y to confirm, N to veto.")
        _record_outbound(sid, fake_key, msg, recipients)
        log.info("test-confirmation sid=%s recipients=%s",
                 sid, ", ".join(recipients))
        ok = send_sms(msg, reply_webhook_url=REPLY_WEBHOOK_URL)
        log.info("test-confirmation via textbelt: %s",
                 "ALL ACCEPTED" if ok else "PARTIAL/FAIL")
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

    # Positions snapshot: every open contract with live price + MTM where
    # we have a locked futures_price. Covers HTA, APP, INVENTORY, BASIS,
    # CASH. The React dashboard reads this to render open positions even
    # when there are no active signals firing.
    positions = build_positions_snapshot()
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2) + "\n")
    log.info("wrote %s — %s positions", POSITIONS_FILE, len(positions["positions"]))

    # Ledger: closed trades and cash receipts (Farm Bridge, etc).
    # Read-through from sales_ledger.json so the dashboard can show
    # YTD bushels sold + payments received without hardcoded values.
    ledger = build_ledger_snapshot()
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2) + "\n")
    log.info("wrote %s", LEDGER_FILE)

    save_state(state)
    log.info("wrote %s", STATE_FILE)

    LASTRUN_FILE.write_text(json.dumps({
        "ran_at":      datetime.now(timezone.utc).isoformat(),
        "signal_count": len(rows),
        "sms_fired":    fired,
        "sms_ready":    bool(ALERT_PHONE and TEXTBELT_KEY),
    }, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
