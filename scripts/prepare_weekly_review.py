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
    inv = 9115
    cash = 3.92
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
    dec = p.get("corn_dec", 4.415)
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
        season = "deep in the summer weather premium — historically the best window for old crop"
    elif month == 7:
        base = 15
        season = "past peak premium, sliding into harvest pressure"
    else:
        base = 20
        season = "standard period"

    reasons = [season]

    # Macro rules (your triggers, in plain language)
    if oil and oil < 80:
        base += 5
        reasons.append("oil is low (less ethanol demand boost — favors selling now)")
    elif oil and oil > 95:
        base -= 5
        reasons.append("oil is strong (ethanol margins better — can wait a bit)")

    if dxy and dxy > 28.3:
        base += 5
        reasons.append("dollar proxy firm (makes U.S. corn pricier for buyers abroad — sell before more pressure)")

    if chg >= 0.10:
        base += 5
        reasons.append("prices popped recently — good moment to capture")

    pct = max(15, min(45, int(base)))
    bu = int(inv * pct / 100)

    # Concise, high-signal summary in layman's terms
    why = " • ".join(reasons[:3])
    return pct, bu, why, oil, dxy

def build_sms(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, inv: int):
    oil_str = f"Oil ${oil:.0f}" if oil else ""
    return (
        f"[FARM] Ritchie Review: Sell {pct}% (~{bu:,} bu) of {inv:,} bu corn.\n"
        f"Dec ${dec:.2f} | Cash ~${cash:.2f} {oil_str}\n"
        f"Why: {why}\n"
        f"Post offers in the trade widget. Full details in the file."
    )

def update_md(pct: int, bu: int, dec: float, cash: float, why: str, oil: float | None, dxy: float | None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next_mon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    inv_local = get_inventory_and_bids()[0]  # for display

    oil_str = f"${oil:.2f}" if oil else "N/A"
    dxy_str = f"{dxy:.2f}" if dxy else "N/A"

    status = f"""## Current Status (auto-generated {today})

**Ritchie corn left**: {inv_local:,} bu

**Time of year**: Summer weather premium window (best historical time for old-crop sales). Hard deadline early July.

**Key numbers**:
- Dec futures: ${dec:.4f}
- Ritchie cash: ~${cash:.2f}
- Oil (CL): {oil_str}
- Dollar proxy: {dxy_str}

**Recommendation**: Sell **{pct}%** (~{bu:,} bu). Use stepped limit offers.

**Why (plain English)**: {why}

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
    print("Done.")
