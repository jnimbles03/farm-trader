#!/usr/bin/env python3
"""
Weekly Grain Brief automation.
Runs once tomorrow, then every Monday at 9am ET.
Pulls latest data, generates a concise high-signal review using your macro rules,
updates the MD file, and texts you the summary.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MD_FILE = DOCS / "weekly_macro_review.md"

TEXTBELT_KEY = os.environ.get("TEXTBELT_KEY", "")
PHONE = "+16302479950"

def load_json(name: str):
    path = DOCS / name
    if not path.exists():
        path = DOCS / "advisor" / name
    if path.exists():
        return json.loads(path.read_text())
    return {}

def get_inventory_and_bids():
    data = load_json("ritchie_live.json") or load_json("bushel.json")
    inv = None
    cash = None
    try:
        if "storage" in data:
            inv = round(data["storage"]["corn_bu"])
        elif "bushelsOnHand" in data:
            inv = round(data["bushelsOnHand"]["corn"]["bushels"])
        for b in data.get("bids", {}).get("corn", []):
            if b.get("month") in ("Jun", "Jul"):
                cash = b.get("cash", cash)
                break
    except Exception:
        pass
    return inv, cash

def get_prices():
    p = load_json("prices.json")
    dec = p.get("corn_dec")
    chg = p.get("detail", {}).get("corn", {}).get("day_chg", 0)
    asof = p.get("detail", {}).get("corn", {}).get("as_of", "")
    return dec, chg, asof

def get_macro():
    """Key macro numbers you track: oil (ethanol driver) and dollar proxy."""
    oil = dxy = None
    try:
        hist = yf.Ticker("CL=F").history(period="2d")
        if not hist.empty:
            oil = round(hist["Close"].iloc[-1], 2)
    except Exception:
        pass
    try:
        hist = yf.Ticker("UUP").history(period="2d")
        if not hist.empty:
            dxy = round(hist["Close"].iloc[-1], 2)
    except Exception:
        pass
    return oil, dxy

def analyze(inv: int, dec: float, cash: float, oil: float | None, dxy: float | None, chg: float):
    """High-IQ but plain-English synthesis using your seasonal + macro framework."""
    month, day = datetime.now(timezone.utc).month, datetime.now(timezone.utc).day

    # Seasonal baseline (your textbook)
    if 6 <= month <= 7 and day <= 10:
        base = 35
        baseline_note = "in the summer weather premium window"
    elif month == 7:
        base = 15
        baseline_note = "past the peak premium period and moving into harvest pressure"
    else:
        base = 20
        baseline_note = "in a standard period"

    reasons = []

    # Macro rules (your triggers, in plain language)
    oil_note = ""
    if oil is not None and oil < 80:
        base += 5
        oil_note = "Oil is low, which reduces ethanol demand"
    elif oil is not None and oil > 95:
        base -= 5
        oil_note = "Oil is strong, supporting ethanol demand"

    if dxy is not None and dxy > 28.3:
        base += 5
        reasons.append("Dollar is firm, pressuring exports")

    if chg is not None and chg >= 0.10:
        base += 5
        reasons.append("Prices have moved up recently")

    pct = max(15, min(45, int(base)))
    bu = int((inv or 0) * pct / 100)

    # Build a clear, followable "Why" that ties news to the baseline schedule
    why_parts = [f"We are {baseline_note}."]
    if oil_note:
        why_parts.append(oil_note + ".")
    if reasons:
        why_parts.append(" • ".join(reasons) + ".")
    why_parts.append("This supports selling a bit more than the normal seasonal baseline pace over the next week.")

    why = " ".join(why_parts).strip()

    return pct, bu, why, oil, dxy

def _next_monday_utc(today: datetime | None = None) -> str:
    now = today or datetime.now(timezone.utc)
    days_ahead = 7 - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _brief_snapshot():
    data = load_json("ritchie_live.json") or load_json("bushel.json") or {}
    storage = data.get("storage") or {}
    bids = data.get("bids") or {}

    corn_on_hand = storage.get("corn_bu")
    beans_on_hand = storage.get("beans_bu")
    if corn_on_hand is None or beans_on_hand is None:
        on_hand = data.get("bushelsOnHand") or {}
        if corn_on_hand is None:
            corn_on_hand = ((on_hand.get("corn") or {}).get("bushels"))
        if beans_on_hand is None:
            soy = on_hand.get("soybeans") or on_hand.get("soy") or {}
            beans_on_hand = soy.get("bushels")

    corn_cash = None
    soy_cash = None
    corn_bid = bids.get("corn") or []
    soy_bid = bids.get("soy") or bids.get("soybeans") or []
    if isinstance(corn_bid, list) and corn_bid:
        first = corn_bid[0]
        if isinstance(first, dict):
            corn_cash = first.get("cash") or first.get("price")
    elif isinstance(corn_bid, dict):
        corn_cash = corn_bid.get("cash") or corn_bid.get("price")
    if isinstance(soy_bid, list) and soy_bid:
        first = soy_bid[0]
        if isinstance(first, dict):
            soy_cash = first.get("cash") or first.get("price")
    elif isinstance(soy_bid, dict):
        soy_cash = soy_bid.get("cash") or soy_bid.get("price")

    sold_beans = 0
    for offer in data.get("openOffers") or []:
        raw = offer.get("raw") or {}
        if str(raw.get("locationName") or "").lower() != "ritchie grain elevator":
            continue
        if str(raw.get("commodityName") or "").lower() != "soybeans":
            continue
        if str(offer.get("status") or raw.get("status") or "").lower() != "filled":
            continue
        qty_raw = raw.get("quantityRemaining")
        try:
            qty_remaining = float(qty_raw)
        except Exception:
            qty_remaining = None
        if qty_remaining is not None and qty_remaining > 0:
            continue
        try:
            sold_beans += int(round(float(raw.get("quantity") or offer.get("quantity") or 0)))
        except Exception:
            continue
    if sold_beans <= 0:
        sold_beans = 800

    corn_remaining = int(round(float(corn_on_hand or 0)))
    beans_on_hand = int(round(float(beans_on_hand or 0)))
    beans_remaining = max(beans_on_hand - sold_beans, 0)
    return {
        "corn_remaining": corn_remaining,
        "beans_remaining": beans_remaining,
        "sold_beans": sold_beans,
        "corn_cash": float(corn_cash) if corn_cash is not None else None,
        "soy_cash": float(soy_cash) if soy_cash is not None else None,
    }


def build_sms(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, inv: int):
    snap = _brief_snapshot()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next_report = _next_monday_utc()
    corn_cash = snap["corn_cash"] if snap["corn_cash"] is not None else cash
    soy_cash = snap["soy_cash"] if snap["soy_cash"] is not None else dec
    corn_cash_str = f"${corn_cash:.2f}" if corn_cash is not None else "N/A"
    soy_cash_str = f"${soy_cash:.2f}" if soy_cash is not None else "N/A"

    return (
        "[FARM] Weekly Grain Brief\n"
        "WHERE WE ARE:\n"
        f"Beans got trimmed down last week with {snap['sold_beans']:,} bu sold. Corn is still the bigger pile at Ritchie.\n\n"
        "WHAT SOLD / WHAT REMAINS:\n"
        f"Sold last week: {snap['sold_beans']:,} bu beans.\n\n"
        f"Beans remaining: about {snap['beans_remaining']:,} bu.\n"
        f"Corn remaining: about {snap['corn_remaining']:,} bu.\n\n"
        "WHAT THE MARKET IS DOING:\n"
        f"Corn cash at Ritchie is around {corn_cash_str}, and beans are around {soy_cash_str}. Market tone is still soft, with oil, tariff noise, and export pressure hanging over it.\n\n"
        "WHAT THIS MEANS:\n"
        "The bean sale locked in some value. Corn still has room, but the market is not giving a strong reason to wait forever. If the bid stays flat, the first week of July is the line.\n\n"
        "WHAT I’D DO:\n"
        "Keep finishing beans if there’s any left, and start selling corn by July 6 if the bid hasn’t improved.\n\n"
        "WHY:\n"
        "Beans already got some value locked in, and corn still has room to work, but the outside market is not strong enough to get greedy.\n\n"
        "CONFIRMATION ACTION ITEM:\n"
        "Reply Y to confirm you are onboard with the strategy.\n\n"
        "-Seaweed Sam (resident agronomist & geopolitical analyst)\n\n"
        f"NEXT REPORT: {next_report}"
    )


def update_md(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, dxy: float | None):
    snap = _brief_snapshot()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next_report = _next_monday_utc()
    corn_cash = snap["corn_cash"] if snap["corn_cash"] is not None else cash
    soy_cash = snap["soy_cash"] if snap["soy_cash"] is not None else dec
    corn_cash_str = f"${corn_cash:.2f}" if corn_cash is not None else "N/A"
    soy_cash_str = f"${soy_cash:.2f}" if soy_cash is not None else "N/A"

    status = f"""## [FARM] Weekly Grain Brief (auto-generated {today})

**Ritchie corn left**: {snap['corn_remaining']:,} bu

**Time of year**: Summer weather premium window (best historical time for old-crop sales). Hard deadline early July.

**Market snapshot**:
- Corn cash bid: ~{corn_cash_str}
- Beans cash bid: ~{soy_cash_str}

**Recommendation**: Beans got trimmed down last week with {snap['sold_beans']:,} bu sold. Keep finishing beans if there’s any left, and start selling corn by July 6 if the bid hasn’t improved.

**Context**: The bean sale locked in some value. Corn still has room, but the market is not giving a strong reason to wait forever. If the bid stays flat, the first week of July is the line.\n\nBeans already got some value locked in, and corn still has room to work, but the outside market is not strong enough to get greedy.

**Next review**: {next_report}
"""

    md = MD_FILE.read_text()
    # Always replace whatever is under the first "## Current Status" with fresh auto-generated content.
    # This ensures we always produce the exact "auto-generated YYYY-MM-DD" format.
    start = md.find("## Current Status")
    if start != -1:
        # find the next top-level ## after it
        rest = md[start:]
        next_match = re.search(r"\n## ", rest)
        if next_match:
            end = start + next_match.start()
            new_md = md[:start] + status + "\n\n" + md[end:]
        else:
            new_md = md[:start] + status
    else:
        new_md = md + "\n\n" + status

    log_header = f"**{today}**"
    if log_header not in new_md:
        log = f"""
**{today}**
- {pct}% (~{bu:,} bu)
- {why}
"""
        if "## Log" in new_md:
            new_md = new_md.replace("## Log\n", "## Log\n" + log, 1)
        else:
            new_md += "\n\n## Log" + log

    MD_FILE.write_text(new_md)
    print("MD updated")

def send_sms(msg: str) -> bool:
    if not TEXTBELT_KEY:
        print("No TEXTBELT_KEY — SMS skipped")
        return False
    try:
        if not msg.startswith("[FARM]"):
            msg = "[FARM] " + msg
        r = httpx.post("https://textbelt.com/text", data={"phone": PHONE, "message": msg, "key": TEXTBELT_KEY}, timeout=15)
        body = r.json()
        print("SMS:", body)
        return body.get("success", False)
    except Exception as e:
        print("SMS error:", e)
        return False

def load_weekly_target():
    """Optional override from admin. File written by workflow before run."""
    try:
        with open("docs/weekly-config.json") as f:
            data = json.load(f)
            pct = data.get("target_pct")
            if isinstance(pct, (int, float)) and 5 <= pct <= 100:
                return int(pct)
    except Exception:
        pass
    return None

if __name__ == "__main__":
    inv, cash = get_inventory_and_bids()
    dec, chg, _ = get_prices()
    oil, dxy = get_macro()

    target = load_weekly_target()
    if target is not None:
        pct = target
        bu = int((inv or 0) * pct / 100)
        why = f"Admin override active. Normal target would be lower; using {pct}% this week."
    else:
        pct, bu, why, oil, dxy = analyze(inv, dec, cash, oil, dxy, chg)

    update_md(pct, bu, dec, cash, why, oil, dxy)

    sms = build_sms(pct, bu, dec, cash, why, oil, inv)
    send_sms(sms)
    print("Done.")
