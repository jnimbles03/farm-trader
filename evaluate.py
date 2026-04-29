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
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
# Dedupe store for news-impact SMS. Records the `id` of every L/XL
# news item we've already texted so the scheduled 15-min run doesn't
# re-alert the same item after each append_news() call.
NEWS_ALERTS_FILE   = STATE_DIR / "news_alerts_sent.json"
PRICES_FILE        = DOCS_DIR / "prices.json"
SIGNALS_FILE       = DOCS_DIR / "signals.json"
POSITIONS_FILE     = DOCS_DIR / "positions.json"
LEDGER_FILE        = DOCS_DIR / "ledger.json"
LASTRUN_FILE       = DOCS_DIR / "last_run.json"
PUBLIC_CONF_FILE   = DOCS_DIR / "confirmations.json"
NEWS_FILE          = DOCS_DIR / "news.json"
PLAN_FILE          = DOCS_DIR / "plan.json"
ORDERS_FILE        = DOCS_DIR / "orders.json"

# How long auto-generated news items stay in the feed before pruning.
# Hand-entered items (no id prefix) are preserved regardless.
# Can be overridden by plan.json "news_retention_days".
DEFAULT_NEWS_RETENTION_DAYS = 30

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
# Shared plan config — single source of truth for commodity tranche plans,
# USDA calendar dates, and market-event thresholds. Lives at docs/plan.json
# so both this pipeline and the browser dashboard (docs/index.html) read
# the same values. Update there, not here.
#
# The constants below are fallbacks used only if plan.json is missing or
# malformed; the real values come from load_plan() at runtime. Keeping
# fallbacks means a corrupted plan file doesn't crash a cron run.
# ---------------------------------------------------------------------------

_DEFAULT_PLAN = {
    "commodities": {
        "corn": {
            "label": "Corn", "oct_low": 4.10, "total_bushels": 12615, "reserve_frac": 0.10,
            "tranches": [
                {"id": "C1", "label": "T1 · Feb–Mar",  "win_start": [2, 1], "win_end": [3, 31], "mult": 1.09, "pct": 25, "note": "Acreage rally"},
                {"id": "C2", "label": "T2 · Apr–May",  "win_start": [4, 1], "win_end": [5, 31], "mult": 1.11, "pct": 25, "note": "Planting premium"},
                {"id": "C3", "label": "T3 · June",     "win_start": [6, 1], "win_end": [6, 30], "mult": 1.12, "pct": 40, "note": "Weather peak"},
                {"id": "C4", "label": "T4 · by Jul 3", "win_start": [7, 1], "win_end": [7,  3], "mult": None, "pct": 10, "note": "Calendar cleanup"},
            ],
        },
        "soybean": {
            "label": "Soybeans", "oct_low": 10.40, "total_bushels": 3217, "reserve_frac": 0.10,
            "tranches": [
                {"id": "S1", "label": "T1 · Feb–Mar", "win_start": [2, 1],  "win_end": [3, 31], "mult": 1.07, "pct": 15, "note": "SA break"},
                {"id": "S2", "label": "T2 · Apr–May", "win_start": [4, 1],  "win_end": [5, 31], "mult": 1.09, "pct": 15, "note": "US planting"},
                {"id": "S3", "label": "T3 · Jun–Jul", "win_start": [6, 1],  "win_end": [7, 15], "mult": 1.10, "pct": 40, "note": "Weather peak"},
                {"id": "S4", "label": "T4 · Jul–Aug", "win_start": [7, 15], "win_end": [8, 15], "mult": 1.08, "pct": 30, "note": "Extended rally"},
            ],
        },
    },
    "usda_calendar": [],     # If missing from plan.json, skip USDA auto-news.
    "big_move_pct": 3.0,
    "news_retention_days": DEFAULT_NEWS_RETENTION_DAYS,
}


def load_plan() -> dict[str, Any]:
    """Read docs/plan.json. Falls back to hardcoded defaults if the file
    is absent or malformed so a bad commit to plan.json doesn't wedge
    the pipeline. Warns loudly on parse errors so it's visible in logs."""
    if not PLAN_FILE.exists():
        log.warning("plan.json missing; using hardcoded defaults")
        return _DEFAULT_PLAN
    try:
        data = json.loads(PLAN_FILE.read_text())
        if not isinstance(data, dict) or "commodities" not in data:
            log.warning("plan.json missing 'commodities' key; using defaults")
            return _DEFAULT_PLAN
        return data
    except json.JSONDecodeError as e:
        log.error("plan.json parse failed, using defaults: %s", e)
        return _DEFAULT_PLAN


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
# News feed — auto-append events + merge with manual entries.
# ---------------------------------------------------------------------------

def _load_news() -> dict[str, Any]:
    """Load docs/news.json. Tolerant of missing / malformed input so a
    corrupt file doesn't kill a scheduled run — we just start fresh."""
    if not NEWS_FILE.exists():
        return {"items": []}
    try:
        data = json.loads(NEWS_FILE.read_text())
        if not isinstance(data, dict) or "items" not in data:
            return {"items": []}
        return data
    except json.JSONDecodeError as e:
        log.warning("news.json is corrupt, resetting: %s", e)
        return {"items": []}


def _save_news(data: dict[str, Any]) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    NEWS_FILE.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def append_news(new_items: list[dict[str, Any]]) -> int:
    """Merge new auto-generated items into news.json.

    Dedup rule: items with the same `id` (our generated IDs) are never
    added twice. Items without an `id` (hand-entered) are preserved
    forever, since we can't reliably match them for de-duplication.

    Pruning: auto-generated items older than the retention window are
    dropped every run. Hand-entered items are never pruned.

    Returns the count of items actually added this run.
    """
    data = _load_news()
    existing = data.get("items", [])
    existing_ids = {it.get("id") for it in existing if it.get("id")}

    added = 0
    for item in new_items:
        if item.get("id") and item["id"] in existing_ids:
            continue
        existing.append(item)
        existing_ids.add(item.get("id"))
        added += 1

    # Retention window sourced from plan.json so it's edit-in-one-place.
    retention = int(load_plan().get("news_retention_days", DEFAULT_NEWS_RETENTION_DAYS))
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=retention)).strftime("%Y-%m-%d")

    kept = []
    for it in existing:
        if not it.get("id"):
            kept.append(it)           # hand-entered — keep forever
            continue
        if (it.get("date") or "") >= cutoff:
            kept.append(it)
        # else: auto-generated + older than cutoff → drop

    # Sort by (impact, date) descending so the file stays roughly ordered
    # even though the dashboard re-sorts on render.
    rank = {"XL": 4, "L": 3, "M": 2, "S": 1}
    kept.sort(key=lambda it: (rank.get(it.get("impact"), 0), it.get("date", "")), reverse=True)

    data["items"] = kept
    _save_news(data)
    return added


# ---------------------------------------------------------------------------
# News-impact SMS — text out when a news item rated L or XL lands
# ---------------------------------------------------------------------------

def _load_news_alerts_sent() -> dict[str, Any]:
    """Track which news ids we've already texted to avoid re-alerting.

    Shape: {"sent_ids": ["usda-2026-04-15-...", ...],
            "last_sent_at": "2026-04-24T18:30:00Z"}

    Missing or malformed file returns an empty, safe default so a scheduled
    run never crashes on corrupt state.
    """
    if not NEWS_ALERTS_FILE.exists():
        return {"sent_ids": [], "last_sent_at": None}
    try:
        data = json.loads(NEWS_ALERTS_FILE.read_text())
        if not isinstance(data, dict) or "sent_ids" not in data:
            return {"sent_ids": [], "last_sent_at": None}
        return data
    except json.JSONDecodeError as e:
        log.warning("news_alerts_sent.json corrupt, resetting: %s", e)
        return {"sent_ids": [], "last_sent_at": None}


def _save_news_alerts_sent(data: dict[str, Any]) -> None:
    NEWS_ALERTS_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _layman_headline(item: dict[str, Any]) -> str:
    """Rewrite the trader-shaped `title` into plain English for SMS.

    The dashboard audience is Jimmy, who reads tranche/front-month/WASDE
    fluently. The SMS audience is broader — spouse, family, ranch hand —
    non-traders in their 30s–40s who just want to know "what happened
    and does it matter?" This translator handles our three auto-news
    sources (Market signal / Market move / USDA) and falls back to the
    original title for hand-entered items.
    """
    title   = (item.get("title") or "").strip()
    source  = (item.get("source") or "").lower()
    affects = item.get("affects") or ""

    commodity_lower = {
        "corn": "corn",
        "soy":  "soybeans",
        "both": "corn and soybeans",
    }.get(affects, "")
    commodity_title = commodity_lower[:1].upper() + commodity_lower[1:] if commodity_lower else "Grain"

    # Seasonal tranche trigger — title looks like
    #   "Corn Tranche 1 trigger hit — front-month at $4.85 (target $4.80)"
    if source == "market signal":
        prices = re.findall(r"\$(\d+\.\d+)", title)
        if len(prices) >= 2:
            current, target = prices[0], prices[1]
            return (f"{commodity_title} hit ${current} today — that's above the "
                    f"${target} we planned to sell some at.")
        if prices:
            return (f"{commodity_title} hit ${prices[0]} today — that's one of "
                    f"the prices we planned to sell some at.")
        return f"{commodity_title} crossed one of our planned sell prices today."

    # Big daily move — title already readable, just normalize
    if source == "market move":
        return title.rstrip(".")

    # USDA calendar report — drop the "USDA:" prefix, translate the
    # cryptic acronyms, add the "this matters because" tail.
    if source == "usda":
        label = title.split(":", 1)[-1].strip() if ":" in title else title
        label = label or "market"
        # Rewrite the few report names that are opaque to non-traders.
        label_map = {
            "wasde": "monthly supply-and-demand (WASDE)",
        }
        label = label_map.get(label.lower(), label)
        target = commodity_lower or "grain"
        return f"USDA just released its {label} report — it can move {target} prices."

    # Hand-entered or unknown source — pass through
    return title


def _direction_hint(item: dict[str, Any]) -> str:
    """One plain-language sentence telling a non-trader which way this
    is pushing prices and whether it's friendly to selling grain. This
    is the single most useful piece of info for a family-member SMS —
    "should I be happy or not?" — so we bolt it on as a second sentence.

    Rules:
      - Market signal (tranche trigger hit): price crossed UP into our
        planned sell zone — unambiguously good for selling.
      - Market move: direction is in the title ("up" / "down"); frame
        as friendly or rough for selling.
      - USDA: direction is unknown pre-release; flag volatility.
    """
    source = (item.get("source") or "").lower()
    title  = (item.get("title") or "").lower()

    if source == "market signal":
        return "Prices are up — this is a planned selling window."

    if source == "market move":
        if re.search(r"\bup\b|rallie|rally|jump|surge|gain", title):
            return "Prices up — friendly day for selling."
        if re.search(r"\bdown\b|drop|fell|fall|loss|weak|tumble|slump|slide", title):
            return "Prices down — tough day for selling."

    if source == "usda":
        return "Could swing prices hard in either direction — watch the close."

    return ""


def _news_alert_message(item: dict[str, Any]) -> str:
    """Build the SMS body for one news item in plain English, aimed at
    a non-trader audience. Impact tier decides the intro ("big news" vs
    "heads up"); _layman_headline scrubs jargon like tranche / front-month
    / WASDE from the title; _direction_hint adds a short second sentence
    so the recipient knows which way prices are moving and what it means
    for selling. Full context over segment cost — 2 SMS segments is fine."""
    impact = (item.get("impact") or "").upper()
    date   = item.get("date") or ""

    intro = {
        "XL": "Freis Farm — big market news: ",
        "L":  "Freis Farm — heads up: ",
    }.get(impact, "Freis Farm — market news: ")

    headline = _layman_headline(item).rstrip(".")

    date_str = ""
    if date:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            date_str = f" ({d.strftime('%b %-d')})"
        except ValueError:
            date_str = f" ({date})"

    hint = _direction_hint(item)
    hint_str = f" {hint}" if hint else ""

    return intro + headline + date_str + "." + hint_str


def send_news_alerts(candidates: list[dict[str, Any]]) -> int:
    """SMS out every L/XL item in `candidates` that we haven't already
    texted. Dedup is by news `id`; items without an id (hand-entered)
    are skipped — we can't safely match them across runs.

    On first run (no state file yet), we seed `sent_ids` from the current
    `news.json` so deploying this feature doesn't retroactively blast
    the past 30 days of L/XL items.

    Returns count of items actually dispatched.
    """
    if not candidates:
        return 0
    if not (ALERT_PHONE and TEXTBELT_KEY):
        log.info("news alerts skipped — ALERT_PHONE or TEXTBELT_KEY not set")
        return 0

    state = _load_news_alerts_sent()
    first_run = not NEWS_ALERTS_FILE.exists()
    sent_ids: set[str] = set(state.get("sent_ids", []))

    # First-run seed: treat every L/XL id currently in news.json as
    # already-alerted. Otherwise the first scheduled run after deploy
    # would text the entire backlog.
    if first_run:
        existing = _load_news().get("items", []) or []
        for it in existing:
            if it.get("id") and (it.get("impact") or "").upper() in ("L", "XL"):
                sent_ids.add(it["id"])
        log.info("news alerts: first-run seed — marked %d existing L/XL items as sent",
                 len(sent_ids))

    fired = 0
    for item in candidates:
        impact = (item.get("impact") or "").upper()
        if impact not in ("L", "XL"):
            continue
        nid = item.get("id")
        if not nid:
            # Hand-entered items have no id — we'd re-alert on every run.
            continue
        if nid in sent_ids:
            continue

        msg = _news_alert_message(item)
        if send_sms(msg):
            sent_ids.add(nid)
            fired += 1
            log.info("news alert fired: %s (%s)", nid, impact)
        else:
            log.warning("news alert SMS failed for %s — will retry next run", nid)

    # Keep the sent list from growing unbounded: cap at roughly 2x the
    # news retention window so rotated-out items drop out eventually.
    retention = int(load_plan().get("news_retention_days", DEFAULT_NEWS_RETENTION_DAYS))
    cap = max(200, retention * 4)
    sent_list = list(sent_ids)
    if len(sent_list) > cap:
        sent_list = sent_list[-cap:]

    _save_news_alerts_sent({
        "sent_ids":     sorted(sent_list),
        "last_sent_at": datetime.now(timezone.utc).isoformat() if fired else state.get("last_sent_at"),
    })
    return fired


def usda_news_for_today() -> list[dict[str, Any]]:
    """Emit a news item for any USDA report whose release date is today.
    Calendar comes from plan.json (falls back to empty list on missing)."""
    now = datetime.now(timezone.utc)
    calendar = load_plan().get("usda_calendar") or []
    out = []
    for rpt in calendar:
        if rpt.get("month") == now.month and rpt.get("day") == now.day:
            label = rpt.get("label", "USDA report")
            out.append({
                "id":      f"usda-{now.year}-{now.month:02d}-{now.day:02d}-{label[:20].replace(' ','_')}",
                "date":    now.strftime("%Y-%m-%d"),
                "title":   f"USDA: {label}",
                "impact":  rpt.get("size", "M"),
                "affects": rpt.get("affects", "both"),
                "source":  "USDA",
            })
    return out


def policy_calendar_news_for_today() -> list[dict[str, Any]]:
    """Emit a news item for any curated policy/geopolitical event scheduled
    for today. Calendar comes from plan.json -> policy_calendar: FOMC
    decisions, EPA RFS deadlines, Brazil/Argentina harvest windows, trade
    policy events, etc.

    Distinct from usda_news_for_today(): those are statutory USDA reports;
    these are the non-USDA market movers the farm operator wants on the
    blotter as they land. Items have a `category` field (policy /
    geopolitical / report) for downstream styling if we want it.

    Year is optional on each entry: omit for events that recur yearly on
    the same (month, day) such as Brazil harvest window; include for
    one-off dates like a specific FOMC meeting."""
    now = datetime.now(timezone.utc)
    calendar = load_plan().get("policy_calendar") or []
    out = []
    for evt in calendar:
        year = evt.get("year")
        if year and year != now.year:
            continue
        if evt.get("month") == now.month and evt.get("day") == now.day:
            label = evt.get("label", "Policy event")
            category = evt.get("category", "policy")
            # id includes the category prefix so USDA / policy / rss items
            # never collide in the dedup set.
            safe = re.sub(r"[^a-z0-9]+", "_", label.lower())[:28].strip("_")
            out.append({
                "id":       f"{category}-{now.year}-{now.month:02d}-{now.day:02d}-{safe}",
                "date":     now.strftime("%Y-%m-%d"),
                "title":    label,
                "impact":   evt.get("size", "M"),
                "affects":  evt.get("affects", "both"),
                "source":   evt.get("source", category.capitalize()),
                "category": category,
            })
    return out


def rss_news_today() -> list[dict[str, Any]]:
    """Fetch configured RSS feeds and return recent ag-relevant items.

    Config comes from plan.json:
      news_feeds:    [{ url, source, max_age_hours }, ...]
      news_keywords: { topic: [...], xl: [...], l: [...], m: [...] }

    Behavior:
      - Fetch each feed with httpx (5s connect / 10s read timeout).
      - Parse as RSS 2.0 <item> first, fall back to Atom <entry>.
      - Item must be younger than feed.max_age_hours.
      - Item must match at least one `topic` keyword (case-insensitive
        substring on title+summary) or it's ignored entirely — we don't
        want generic USDA press releases about school lunches.
      - Impact is the highest tier (xl > l > m > s) whose keyword list
        contains a match. Tiered first-match-wins.
      - `affects` is derived from commodity keywords in the text:
        corn-only, soy-only, both, or both if neither mentioned.
      - Id is `rss-<source>-<sha1(title|date)[:10]>` so `append_news()`
        dedups across runs even if pubDate drifts a bit.
      - Any network or parse failure is logged and skipped: one dead
        feed cannot kill the evaluate run."""
    plan = load_plan()
    feeds = plan.get("news_feeds") or []
    kw    = plan.get("news_keywords") or {}
    topic_terms = [t.lower() for t in kw.get("topic", [])]
    xl_terms    = [t.lower() for t in kw.get("xl", [])]
    l_terms     = [t.lower() for t in kw.get("l", [])]
    m_terms     = [t.lower() for t in kw.get("m", [])]

    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for feed_cfg in feeds:
        url = feed_cfg.get("url")
        if not url:
            continue
        source = feed_cfg.get("source", "RSS")
        max_age_hours = int(feed_cfg.get("max_age_hours", 48))

        try:
            resp = httpx.get(url, timeout=httpx.Timeout(10.0, connect=5.0),
                             headers={"User-Agent": "freis-farm-blotter/1.0"})
            resp.raise_for_status()
            xml_bytes = resp.content
        except Exception as e:
            log.warning("news rss: fetch failed for %s (%s): %s", source, url, e)
            continue

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            log.warning("news rss: parse failed for %s: %s", source, e)
            continue

        # RSS 2.0: <rss><channel><item>. Atom: <feed><entry>. Namespace
        # varies on Atom; strip it by searching with local-name.
        items = root.findall(".//item")
        atom_ns = "{http://www.w3.org/2005/Atom}"
        if not items:
            items = root.findall(f".//{atom_ns}entry")

        for it in items:
            title = (
                it.findtext("title")
                or it.findtext(f"{atom_ns}title")
                or ""
            ).strip()
            if not title:
                continue

            summary = (
                it.findtext("description")
                or it.findtext(f"{atom_ns}summary")
                or ""
            ).strip()

            pub = (
                it.findtext("pubDate")
                or it.findtext(f"{atom_ns}updated")
                or it.findtext(f"{atom_ns}published")
                or ""
            ).strip()
            try:
                pub_dt = parsedate_to_datetime(pub) if pub else now
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                # ISO 8601 fallback (Atom feeds)
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = now
            age_hours = (now - pub_dt).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                continue

            text = (title + " " + summary).lower()

            # Topic filter: skip if no ag-relevant word appears
            if topic_terms and not any(t in text for t in topic_terms):
                continue

            # Tiered impact scoring
            if any(t in text for t in xl_terms):
                impact = "XL"
            elif any(t in text for t in l_terms):
                impact = "L"
            elif any(t in text for t in m_terms):
                impact = "M"
            else:
                impact = "S"

            # Commodity attribution from keywords
            has_corn = "corn" in text
            has_soy  = ("soy" in text) or ("soybean" in text)
            if has_corn and has_soy:
                affects = "both"
            elif has_corn:
                affects = "corn"
            elif has_soy:
                affects = "soy"
            else:
                affects = "both"  # generic ag news applies to both

            # Stable dedup id
            source_slug = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")
            h = hashlib.sha1(f"{title}|{pub_dt.strftime('%Y-%m-%d')}".encode("utf-8")).hexdigest()[:10]
            out.append({
                "id":       f"rss-{source_slug}-{h}",
                "date":     pub_dt.strftime("%Y-%m-%d"),
                "title":    title[:180],
                "impact":   impact,
                "affects":  affects,
                "source":   source,
                "category": "rss",
            })

    return out


def seasonal_trigger_news_for(commodity: str, live: float | None) -> list[dict[str, Any]]:
    """DEPRECATED as a news source — kept for reference.

    Check each seasonal tranche trigger for `commodity`. Emit a news
    item for any tranche whose trigger price has been crossed today but
    whose corresponding news item isn't already in the feed.

    Removed from news_events assembly on 2026-04-24: tranche hits
    already render as SELL-NOW pills in the Decision Aid, so duplicating
    them on the blotter was noise. Kept here in case we want to revive
    it later as a different channel (e.g. SMS-only).

    Commodity config and tranche multipliers come from plan.json.
    ID scheme: `trigger-<commodity>-<tranche_id>-<crop_year>`. The crop
    year keeps the same trigger from re-firing news year after year."""
    plan = load_plan().get("commodities", {}).get(commodity)
    if not plan or live is None:
        return []
    oct_low = plan.get("oct_low")
    if oct_low is None:
        return []
    crop_year = datetime.now(timezone.utc).year
    out = []
    for tr in plan.get("tranches", []):
        if tr.get("mult") is None:
            continue  # Calendar-only tranche, no trigger price.
        target = round(oct_low * tr["mult"], 4)
        if live >= target:
            out.append({
                "id":      f"trigger-{commodity}-{tr['id']}-{crop_year}",
                "date":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "title":   f"{plan.get('label', commodity)} {tr.get('label', tr['id'])} trigger hit — front-month at ${live:.2f} (target ${target:.2f})",
                "impact":  "L",
                "affects": "corn" if commodity == "corn" else "soy",
                "source":  "Market signal",
                "note":    tr.get("note", ""),
            })
    return out


def big_move_news_for(commodity: str, detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    """If a commodity moved more than the plan's big_move_pct today, emit
    an item. Uses the day_chg_pct computed by _price_detail()."""
    if not detail or detail.get("day_chg_pct") is None:
        return []
    threshold = float(load_plan().get("big_move_pct", 3.0))
    pct = detail["day_chg_pct"]
    if abs(pct) < threshold:
        return []
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    direction = "up" if pct > 0 else "down"
    label = "Corn" if commodity == "corn" else ("Soybeans" if commodity == "soy" else commodity.capitalize())
    impact = "L" if abs(pct) >= 5 else "M"
    return [{
        "id":      f"bigmove-{commodity}-{today_iso}-{direction}",
        "date":    today_iso,
        "title":   f"{label} {direction} {abs(pct):.1f}% today",
        "impact":  impact,
        "affects": "corn" if commodity == "corn" else "soy",
        "source":  "Market move",
    }]


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


def load_orders() -> dict[str, Any]:
    """Read docs/orders.json. Returns the canonical {orders: [...]} shape
    or an empty default if the file is missing/corrupt — a bad orders
    file should not wedge the price pipeline."""
    if not ORDERS_FILE.exists():
        return {"orders": []}
    try:
        data = json.loads(ORDERS_FILE.read_text())
        if not isinstance(data, dict) or "orders" not in data:
            log.warning("orders.json missing 'orders' key; treating as empty")
            return {"orders": []}
        return data
    except json.JSONDecodeError as e:
        log.warning("orders.json corrupt, treating as empty: %s", e)
        return {"orders": []}


def summarize_orders(orders: list[dict[str, Any]]) -> dict[str, int]:
    """Tally orders by status for last_run.json visibility."""
    out: dict[str, int] = {"draft": 0, "live": 0, "filled": 0,
                           "cancelled": 0, "expired": 0, "other": 0}
    for o in orders:
        s = o.get("status", "other")
        if s in out:
            out[s] += 1
        else:
            out["other"] += 1
    return out


def process_live_orders(orders: list[dict[str, Any]],
                        prices: dict[str, Any]) -> int:
    """Reserved hook for SMS-firing limit-order triggers.

    Per current product policy (2026-04-28), all incoming orders land as
    `status: "draft"` and are NEVER auto-promoted. Drafts are inert: this
    function deliberately ignores them. When a draft is manually promoted
    to `status: "live"` (a JSON edit, or a future "promote" UI), this is
    the place that will compare each live limit order against the latest
    bid and fire an SMS-confirmation through the existing /reply pipeline.

    Returns the number of orders that triggered an alert this run. For
    now: always 0, since drafts are skipped and there are no live rows.
    """
    fired = 0
    for o in orders:
        if o.get("status") != "live":
            continue
        # Hook for future logic — left intentionally empty so a stray
        # `live` row doesn't surprise anyone with an SMS until we
        # explicitly wire this up. Log loudly so we notice if it lands.
        log.warning(
            "orders.json has a live row (%s) — live-order firing is not "
            "yet wired. Manually update or revert to draft.",
            o.get("id", "<no-id>"),
        )
    return fired


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

    # Auto-news: the blotter is report announcements, policy events, and
    # geopolitical news — NOT price prints. Price-trigger hits already
    # show as SELL-NOW pills in the Decision Aid and big daily moves
    # show as ▲/▼ on the price strip; duplicating them on the blotter
    # was noise.
    #
    # Three sources:
    #   1. USDA statutory calendar          (plan.json -> usda_calendar)
    #   2. Curated policy/geopolitical cal  (plan.json -> policy_calendar)
    #   3. Live RSS feeds                   (plan.json -> news_feeds)
    #
    # append_news() de-duplicates by id and prunes by retention days.
    # Hand-entered items (no id) are preserved forever.
    news_events = []
    news_events.extend(usda_news_for_today())
    news_events.extend(policy_calendar_news_for_today())
    try:
        news_events.extend(rss_news_today())
    except Exception as e:
        # Belt-and-suspenders: rss_news_today() swallows per-feed errors,
        # but if something unexpected raises at the call-site we still
        # don't want to abort the whole evaluate run.
        log.warning("news rss: unexpected error, skipping this run: %s", e)
    added = append_news(news_events)
    log.info("news.json: %d event(s) emitted, %d added after de-dup", len(news_events), added)

    # Auto-SMS any news item rated L or XL. Dedup state in
    # state/news_alerts_sent.json so a given item only texts once,
    # even though evaluate.py re-emits trigger/big-move news on every
    # 15-min run while conditions persist.
    news_sms_fired = send_news_alerts(news_events)
    if news_sms_fired:
        log.info("news.json: dispatched %d L/XL news alert(s)", news_sms_fired)

    save_state(state)
    log.info("wrote %s", STATE_FILE)

    # Orders snapshot — accept_order.py is what writes docs/orders.json,
    # so evaluate.py just READS it to (a) tally drafts vs live for the
    # last_run summary and (b) call process_live_orders() which is
    # currently a no-op guard. Drafts never fire alerts.
    orders_data = load_orders()
    order_rows  = orders_data.get("orders", [])
    order_tally = summarize_orders(order_rows)
    order_alerts = process_live_orders(order_rows, prices)
    log.info("orders.json: %s (alerts fired: %d)", order_tally, order_alerts)

    LASTRUN_FILE.write_text(json.dumps({
        "ran_at":       datetime.now(timezone.utc).isoformat(),
        "signal_count": len(rows),
        "sms_fired":    fired,
        "sms_ready":    bool(ALERT_PHONE and TEXTBELT_KEY),
        "orders":       order_tally,
        "order_alerts": order_alerts,
    }, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
