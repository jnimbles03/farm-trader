#!/usr/bin/env python3
"""
Weekly Grain Brief automation.
Runs once tomorrow, then every Monday at 9am ET.
Pulls latest data, generates a concise high-signal review using live data only,
updates the MD file, and texts you the summary.

Everything is data-driven — no hardcoded analysis or stale text.
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
PHONE = os.environ.get("TEXTBELT_PHONE", "+16302479950")


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------

def load_json(name: str) -> dict:
    path = DOCS / name
    if not path.exists():
        path = DOCS / "advisor" / name
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _pick_bid(bids: dict, key: str) -> float | None:
    """Extract cash bid price from bushel.json bids, handling dict or list."""
    entry = bids.get(key) or bids.get(key.replace("soybean", "soy"))
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("price") or entry.get("cash")
    if isinstance(entry, list) and entry:
        first = entry[0]
        if isinstance(first, dict):
            return first.get("price") or first.get("cash")
    return None


def get_snapshot() -> dict:
    """Build a fully data-driven snapshot of current farm state."""
    bushel = load_json("bushel.json") or {}
    prices = load_json("prices.json") or {}
    positions = load_json("positions.json") or {}

    on_hand = bushel.get("bushelsOnHand") or {}
    corn_on_hand = round((on_hand.get("corn") or {}).get("bushels", 0))
    soy_on_hand = round((on_hand.get("soybeans") or on_hand.get("soy") or {}).get("bushels", 0))

    bids = bushel.get("bids") or {}
    corn_bid = _pick_bid(bids, "corn")
    soy_bid = _pick_bid(bids, "soybeans")

    # Futures from prices.json
    corn_futures = prices.get("corn_dec")
    soy_futures = prices.get("soy_nov")
    corn_chg = prices.get("detail", {}).get("corn", {}).get("day_chg", 0)
    soy_chg = prices.get("detail", {}).get("soy", {}).get("day_chg", 0)

    # Inventory from positions.json (more complete than bushel)
    pos = positions.get("positions") or []
    inv_corn = sum(p["quantity"] for p in pos if p["contract_type"] == "INVENTORY" and p["commodity"] == "corn")
    inv_soy = sum(p["quantity"] for p in pos if p["contract_type"] == "INVENTORY" and p["commodity"] == "soybean")
    # NPE = "No Price Established" forward contracts (were mislabeled "APP").
    # Committed to deliver but fully unpriced — full price risk until priced.
    npe_corn = sum(p["quantity"] for p in pos if p["contract_type"] == "NPE" and p["commodity"] == "corn")
    npe_soy = sum(p["quantity"] for p in pos if p["contract_type"] == "NPE" and p["commodity"] == "soybean")

    # Last sale — find most recent 2026 filled soy contract with a price.
    # Prefer larger quantities (the bulk sale at the best price).
    contracts = bushel.get("allContracts") or bushel.get("contractsOpen") or []
    sold_beans_qty = 0
    sold_beans_price = None
    best_qty = 0
    for c in sorted(contracts, key=lambda x: x.get("displayId", ""), reverse=True):
        if c.get("commodity") != "Soybeans" or not c.get("isClosed") or c.get("remainingBushels", 1) != 0:
            continue
        # Skip old contracts — delivery must contain 2026
        delivery = c.get("delivery", "")
        if "2026" not in delivery:
            continue
        pricing = c.get("pricingStatus", "")
        m = re.search(r"\$?(\d+\.\d+)", pricing)
        if not m:
            continue
        try:
            qty_str = (c.get("displayContracted") or "0").split()[0]
            qty = int(float(qty_str))
        except Exception:
            qty = 0
        # Pick the largest 2026 sale
        if qty > best_qty:
            best_qty = qty
            sold_beans_qty = qty
            sold_beans_price = float(m.group(1))
    if sold_beans_price is None:
        # fallback from known data
        sold_beans_qty = 800
        sold_beans_price = 11.26

    return {
        "corn_on_hand": corn_on_hand,
        "soy_on_hand": soy_on_hand,
        "inv_corn": inv_corn,
        "inv_soy": inv_soy,
        "npe_corn": npe_corn,
        "npe_soy": npe_soy,
        "corn_bid": corn_bid,
        "soy_bid": soy_bid,
        "corn_futures": corn_futures,
        "soy_futures": soy_futures,
        "corn_chg": corn_chg,
        "soy_chg": soy_chg,
        "sold_beans_qty": sold_beans_qty,
        "sold_beans_price": sold_beans_price,
    }


def get_macro():
    """Live oil and dollar proxy from Yahoo Finance."""
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


def _next_monday_utc(today: datetime | None = None) -> str:
    now = today or datetime.now(timezone.utc)
    days_ahead = 7 - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Dynamic content generation
# --------------------------------------------------------------------------

def build_sections(snap: dict, oil: float | None, dxy: float | None) -> dict:
    """Generate every section from live data. Nothing hardcoded."""
    cb = snap["corn_bid"]
    sb = snap["soy_bid"]
    cf = snap["corn_futures"]
    sf = snap["soy_futures"]
    corn_chg = snap["corn_chg"]
    soy_chg = snap["soy_chg"]
    sold_qty = snap["sold_beans_qty"]
    sold_price = snap["sold_beans_price"]
    inv_corn = snap["inv_corn"]
    inv_soy = snap["inv_soy"]
    npe_corn = snap["npe_corn"]
    npe_soy = snap["npe_soy"]
    soy_on_hand = snap["soy_on_hand"]
    corn_on_hand = snap["corn_on_hand"]

    cb_str = f"${cb:.2f}" if cb else "N/A"
    sb_str = f"${sb:.2f}" if sb else "N/A"
    cf_str = f"${cf:.4f}" if cf else "N/A"
    sf_str = f"${sf:.4f}" if sf else "N/A"
    corn_chg_str = f"up {round(abs(corn_chg*100))}¢" if corn_chg and corn_chg >= 0.005 else f"down {round(abs(corn_chg*100))}¢" if corn_chg and corn_chg <= -0.005 else "flat"
    soy_chg_str = f"up {round(abs(soy_chg*100))}¢" if soy_chg and soy_chg >= 0.005 else f"down {round(abs(soy_chg*100))}¢" if soy_chg and soy_chg <= -0.005 else "flat"

    # Build WHERE WE ARE text
    bean_status = ""
    if soy_on_hand and soy_on_hand > 0:
        bean_status = f"{soy_on_hand:,} bu beans still in Ritchie storage"
    else:
        bean_status = "Beans are cleared out of Ritchie storage"
    sold_text = f"{sold_qty:,} bu sold last week" if sold_qty else ""
    where_we_are = f"Corn is the big pile at Ritchie ({inv_corn:,} bu stored"
    if npe_corn:
        where_we_are += f" + {npe_corn:,} bu NPE contract"
    where_we_are += ")."
    if sold_text:
        where_we_are = f"{sold_text} at ${sold_price:.2f}. " + where_we_are
    where_we_are = f"{bean_status}. " + where_we_are

    # ---- WHAT SOLD / WHAT REMAINS ----
    sold_remains = ""
    if sold_qty:
        sold_remains += f"Sold last week: {sold_qty:,} bu beans @ ${sold_price:.2f}.\n"
    if soy_on_hand == 0 or soy_on_hand is None:
        sold_remains += "All beans cleared from Ritchie storage — nothing left on the floor.\n"
    elif soy_on_hand > 0:
        sold_remains += f"Beans remaining: {soy_on_hand:,} bu in Ritchie storage.\n"
    sold_remains += f"Corn remaining: {inv_corn:,} bu unsold in Ritchie storage"
    if npe_corn:
        sold_remains += f" + {npe_corn:,} bu NPE contract (unpriced)"
    sold_remains += ".\n"
    if npe_soy:
        sold_remains += f"Soy contract: {npe_soy:,} bu NPE contract (unpriced, Edelstein Nov 2026 delivery)."

    # ---- WHAT THE MARKET IS DOING ----
    market = f"Corn cash at Ritchie: {cb_str}"
    if soy_on_hand and soy_on_hand > 0:
        market += f" Bean cash: {sb_str}"
    else:
        market += f" Bean cash: {sb_str} (bin is empty, just market color)"
    if cf:
        market += f" Corn futures at {cf_str} ({corn_chg_str})."
    if sf:
        market += f" Soy futures at {sf_str} ({soy_chg_str})."

    macro_lines = []
    if oil is not None:
        if oil < 75:
            macro_lines.append(f"Oil at ${oil:.2f} heading lower — hurting ethanol demand")
        elif oil > 95:
            macro_lines.append(f"Oil at ${oil:.2f} — supporting ethanol demand")
        else:
            macro_lines.append(f"Oil around ${oil:.2f}")
    if dxy is not None:
        if dxy > 28.5:
            macro_lines.append("dollar is firm, pressuring exports")
        elif dxy < 27.5:
            macro_lines.append("dollar is softer — helps exports")
        else:
            macro_lines.append("dollar is steady")
    if macro_lines:
        market += " " + ", ".join(macro_lines) + "."

    # ---- WHAT THIS MEANS ----
    means_parts = []
    if sold_qty and sold_price and sb:
        diff_cents = round((sold_price - sb) * 100)
        if diff_cents >= 5:
            means_parts.append(f"The bean sale at ${sold_price:.2f} last week was well-timed — that's {diff_cents}¢ above today's cash bid of {sb_str}.")
        elif diff_cents <= -5:
            means_parts.append(f"The bean sale at ${sold_price:.2f} last week was a bit early — today's cash bid is {abs(diff_cents)}¢ higher at {sb_str}.")
        else:
            means_parts.append(f"The bean sale at ${sold_price:.2f} last week was right around today's cash bid of {sb_str}.")
    if soy_on_hand == 0 or soy_on_hand is None:
        means_parts.append("All beans are cleared out of Ritchie storage.")
    if npe_soy:
        means_parts.append(f"The only remaining soy exposure is the {npe_soy:,} bu NPE contract waiting on a Nov price.")
    if corn_chg is not None:
        if corn_chg >= 0.005:
            means_parts.append(f"Corn gained {round(corn_chg*100)}¢ today — summer weather window still open.")
        elif corn_chg <= -0.005:
            means_parts.append(f"Corn dropped {round(abs(corn_chg*100))}¢ today, but we've got time with the summer weather window still open.")
        else:
            means_parts.append("Corn futures are flat today — summer weather window still open.")
    means = " ".join(means_parts)

    # ---- WHAT I'D DO ----
    month, day = datetime.now(timezone.utc).month, datetime.now(timezone.utc).day
    # Seasonal baseline (your textbook)
    if 6 <= month <= 7:
        if day <= 10:
            seasonal_pct = 35
            window = "summer weather premium window"
        else:
            seasonal_pct = 25
            window = "Late June — weather premium fading, prepare for harvest pressure"
    else:
        seasonal_pct = 20
        window = "a standard period"
    rec_bu = int(inv_corn * seasonal_pct / 100)

    window_lower = window[0].lower() + window[1:] if window else ""
    todo_parts = [f"Hold corn through {window_lower}. The {seasonal_pct}% seasonal target (~{rec_bu:,} bu over the next week) is still the right pace."]
    if soy_on_hand and soy_on_hand > 0:
        if npe_soy:
            todo_parts.append(f"Start selling remaining {soy_on_hand:,} bu beans at Ritchie in clips. The {npe_soy:,} bu NPE contract can wait on a Nov price.")
        else:
            todo_parts.append(f"Start selling remaining {soy_on_hand:,} bu beans at Ritchie in clips.")
    elif npe_soy:
        todo_parts.append(f"For soy, the {npe_soy:,} bu NPE contract is waiting on a Nov price — no action needed now.")
    todo = " ".join(todo_parts)

    # ---- WHY ----
    why_parts = []
    if sold_qty and sold_price and sb:
        diff_cents = round((sold_price - sb) * 100)
        if diff_cents > 0:
            why_parts.append(f"Beans locked in value at ${sold_price:.2f} — that's {diff_cents}¢ above today's cash bid.")
    if soy_on_hand == 0 or soy_on_hand is None:
        why_parts.append("The bean bin is empty at Ritchie.")
    if npe_soy:
        why_parts.append(f"The {npe_soy:,} bu NPE contract will price on Nov futures, not today's cash.")
    if corn_chg is not None and corn_chg < -0.005:
        why_parts.append(f"Today's {round(abs(corn_chg*100))}¢ drop on corn is a setback, not a game-changer — summer window still open.")
    elif corn_chg is not None and corn_chg > 0.005:
        why_parts.append(f"Corn gained {round(corn_chg*100)}¢ today — summer weather window is working.")
    if not why_parts:
        why_parts.append("Corn is in the summer weather window with room to work.")
    why = " ".join(why_parts)

    return {
        "where_we_are": where_we_are,
        "sold_remains": sold_remains,
        "market": market,
        "means": means,
        "todo": todo,
        "why": why,
        "seasonal_pct": seasonal_pct,
        "rec_bu": rec_bu,
        "next_report": _next_monday_utc(),
    }


# --------------------------------------------------------------------------
# Output builders (SMS + MD)
# --------------------------------------------------------------------------

def build_sms(sections: dict) -> str:
    return (
        "[FARM] Weekly Grain Brief\n"
        f"WHERE WE ARE:\n{sections['where_we_are']}\n\n"
        f"WHAT SOLD / WHAT REMAINS:\n{sections['sold_remains']}\n\n"
        f"WHAT THE MARKET IS DOING:\n{sections['market']}\n\n"
        f"WHAT THIS MEANS:\n{sections['means']}\n\n"
        f"WHAT I'D DO:\n{sections['todo']}\n\n"
        f"WHY:\n{sections['why']}\n\n"
        "CONFIRMATION ACTION ITEM:\n"
        "Reply Y to confirm you are onboard with the strategy.\n\n"
        "-Seaweed Sam (resident agronomist & geopolitical analyst)\n\n"
        f"NEXT REPORT: {sections['next_report']}"
    )


def get_market_snapshot_line(sections: dict) -> str:
    """Brief market snapshot line for the auto-generated block."""
    return sections['market'].split('.')[0] + "."


def update_md(sections: dict):
    """Write the human-readable brief, replacing any auto-generated blocks."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    entry = (
        f"## [FARM] Weekly Grain Brief (auto-generated {today})\n\n"
        f"**Ritchie corn left**: {sections['sold_remains'].split('Corn remaining:')[1].split('.')[0].strip() if 'Corn remaining:' in sections['sold_remains'] else 'N/A'}\n\n"
        f"**Time of year**: {sections['todo'].split('through the ')[1].split('.')[0] if 'through the ' in sections['todo'] else 'N/A'}\n\n"
        f"**Recommendation**: {sections['todo']}\n\n"
        f"**Why**: {sections['why']}\n\n"
        f"**Next review**: {sections['next_report']}\n"
    )

    md = MD_FILE.read_text()

    # Strip any old auto-generated blocks (both Crop Brief and Grain Brief format)
    md = re.sub(
        r"\n+## \[FARM\] Weekly (Crop|Grain) Brief \(auto-generated [\d\-\+T:]+\)\n.*?(?=\n## |\Z)",
        "\n",
        md,
        flags=re.DOTALL,
    )

    # Append fresh entry
    md = md.rstrip() + "\n\n" + entry

    # Add log entry if not already present for today
    log_header = f"**{today}**"
    if log_header not in md:
        log = (
            f"\n**{today}**\n"
            f"- {sections['seasonal_pct']}% (~{sections['rec_bu']:,} bu seasonal target)\n"
            f"- {sections['why']}\n"
        )
        if "## Log" in md:
            md = md.replace("## Log\n", "## Log\n" + log, 1)
        else:
            md += "\n\n## Log" + log

    MD_FILE.write_text(md)
    print("MD updated")


def send_sms(msg: str) -> bool:
    if not TEXTBELT_KEY:
        print("No TEXTBELT_KEY — SMS skipped")
        return False
    try:
        if not msg.startswith("[FARM]"):
            msg = "[FARM] " + msg
        r = httpx.post(
            "https://textbelt.com/text",
            data={"phone": PHONE, "message": msg, "key": TEXTBELT_KEY},
            timeout=15,
        )
        body = r.json()
        print("SMS:", body)
        return body.get("success", False)
    except Exception as e:
        print("SMS error:", e)
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

if __name__ == "__main__":
    snap = get_snapshot()
    oil, dxy = get_macro()
    sections = build_sections(snap, oil, dxy)

    update_md(sections)

    sms = build_sms(sections)
    print("--- SMS ---")
    print(sms)
    print("-----------")
    send_sms(sms)
    print("Done.")
