#!/usr/bin/env python3
"""
Weekly Corn Selling Review automation (Monday 9am ET).
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

    why = " ".join(why_parts).strip()

    return pct, bu, why, oil, dxy

def build_sms(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, inv: int):
    dec_str = f"${dec:.2f}" if dec is not None else "N/A"
    cash_str = f"${cash:.2f}" if cash is not None else "N/A"
    return (
        "[FARM] Weekly Crop Brief\n\n"
        f"Recommended we sell {pct}% of the crop in the {cash_str}–{dec_str} range over the next week.\n\n"
        f"Market snapshot: Futures {dec_str} | Ritchie cash ~{cash_str}\n\n"
        f"Context: {why}\n\n"
        "Reply to chat with Seaweed Sam.\n\n"
        "-Seaweed Sam (Your personal pro farmer)"
    )

def update_md(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, dxy: float | None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next_mon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    inv_local = get_inventory_and_bids()[0] or 0

    oil_str = f"${oil:.2f}" if oil else "N/A"
    dxy_str = f"{dxy:.2f}" if dxy else "N/A"
    dec_str = f"${dec:.4f}" if dec is not None else "N/A"
    cash_str = f"${cash:.2f}" if cash is not None else "N/A"

    status = f"""## [FARM] Weekly Crop Brief (auto-generated {today})

**Ritchie corn left**: {inv_local:,} bu

**Time of year**: Summer weather premium window (best historical time for old-crop sales). Hard deadline early July.

**Market snapshot**:
- Futures price: {dec_str}
- Ritchie cash bid: ~{cash_str}
- Oil (CL): {oil_str}
- Dollar proxy: {dxy_str}

**Recommendation**: Sell **{pct}%** (~{bu:,} bu) in the {cash_str}–{dec_str} range over the next week.

**Context**: {why}

**Next review**: {next_mon}
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

if __name__ == "__main__":
    inv, cash = get_inventory_and_bids()
    dec, chg, _ = get_prices()
    oil, dxy = get_macro()

    pct, bu, why, oil, dxy = analyze(inv, dec, cash, oil, dxy, chg)

    update_md(pct, bu, dec, cash, why, oil, dxy)

    sms = build_sms(pct, bu, dec, cash, why, oil, inv)
    send_sms(sms)
    # Optional: also send a short invitation if wanted, but keep one message for now.
    print("Done.")
