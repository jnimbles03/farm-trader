#!/usr/bin/env python3
"""
Freis Farm weekly report generator.

Generates a concise, narrative weekly brief and sends it as an SMS to a
curated list of recipients. Designed to be run as a GitHub Actions cron
job every Monday morning.

Pulls data from:
  - docs/plan.json: The canonical source for the selling plan, tranche
    windows, and event calendars.
  - docs/bushel.json: Live inventory data from the Bushel platform.
  - yfinance: For live market data (futures, oil, dollar index).

The brief structure is hardcoded in this script to follow the user-provided
template, but the content is dynamically generated based on the latest data.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yfinance as yf

# ---------------------------------------------------------------------------
# Paths and Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PLAN_FILE = ROOT / "docs" / "plan.json"
BUSHEL_FILE = ROOT / "docs" / "bushel.json"

TEXTBELT_KEY = os.environ.get("TEXTBELT_KEY", "")
WEEKLY_REPORT_PHONES = os.environ.get("WEEKLY_REPORT_PHONES", "")


# ---------------------------------------------------------------------------
# Data Models and Loading
# ---------------------------------------------------------------------------

@dataclass
class Tranche:
    id: str
    label: str
    win_start: datetime
    win_end: datetime
    pct: int
    note: str

def load_plan() -> dict:
    """Loads and parses the docs/plan.json file."""
    if not PLAN_FILE.exists():
        raise FileNotFoundError(f"{PLAN_FILE} not found.")
    plan_data = json.loads(PLAN_FILE.read_text())

    # Parse date ranges for corn tranches
    today = datetime.now(timezone.utc).date()
    for t in plan_data.get("commodities", {}).get("corn", {}).get("tranches", []):
        start_month, start_day = t["win_start"]
        end_month, end_day = t["win_end"]
        t["win_start_dt"] = datetime(today.year, start_month, start_day).date()
        t["win_end_dt"] = datetime(today.year, end_month, end_day).date()
    return plan_data


def get_corn_inventory() -> int:
    """Gets remaining corn bushels from the Bushel data file."""
    if not BUSHEL_FILE.exists():
        return 0
    bushel_data = json.loads(BUSHEL_FILE.read_text())
    return int(bushel_data.get("bushelsOnHand", {}).get("corn", {}).get("bushels", 0))


def get_market_data() -> dict:
    """Fetches live market data for futures, oil, and the dollar index."""
    corn_ticker = yf.Ticker("ZC=F")
    oil_ticker = yf.Ticker("CL=F")
    dxy_ticker = yf.Ticker("DX-Y.NYB")

    return {
        "futures": corn_ticker.fast_info.get("last_price", 0.0),
        "oil": oil_ticker.fast_info.get("last_price", 0.0),
        "dxy": dxy_ticker.fast_info.get("last_price", 0.0),
    }

def get_cash_bid() -> float:
    """Gets the cash bid from the Bushel data file."""
    if not BUSHEL_FILE.exists():
        return 0.0
    bushel_data = json.loads(BUSHEL_FILE.read_text())
    # This path is a guess based on the file structure. Adjust if necessary.
    return float(bushel_data.get("cashBids", [{}])[0].get("price", 0.0))


# ---------------------------------------------------------------------------
# Report Logic
# ---------------------------------------------------------------------------

def find_next_tranche(plan: dict) -> dict | None:
    """Finds the next open or upcoming corn tranche."""
    today = datetime.now(timezone.utc).date()
    tranches = plan.get("commodities", {}).get("corn", {}).get("tranches", [])

    # Find the first tranche whose window is currently open or in the future
    for t in sorted(tranches, key=lambda x: x["win_start_dt"]):
        if today <= t["win_end_dt"]:
            return t
    return None


def find_upcoming_events(plan: dict, days_ahead: int = 7) -> list[str]:
    """Finds relevant calendar events in the next week."""
    today = datetime.now(timezone.utc).date()
    lookahead_end = today + timedelta(days=days_ahead)
    events = []

    all_events = plan.get("usda_calendar", []) + plan.get("policy_calendar", [])

    for event in all_events:
        event_year = event.get("year", today.year)
        event_date = datetime(event_year, event["month"], event["day"]).date()
        if today <= event_date <= lookahead_end:
            events.append(f"- {event['label']} ({event_date.strftime('%b %d')})")
    
    return events

# ---------------------------------------------------------------------------
# SMS Generation and Sending
# ---------------------------------------------------------------------------

def generate_brief(
    corn_left: int,
    next_tranche: dict | None,
    market_data: dict,
    cash_bid: float,
    upcoming_events: list[str],
) -> str:
    """Assembles the final brief text from the template and live data."""

    # --- WHERE WE ARE ---
    if next_tranche:
        window_start = next_tranche["win_start_dt"].strftime("%B %d")
        window_end = next_tranche["win_end_dt"].strftime("%B %d")
        timing_window = f"The current selling window is {window_start} to {window_end}."
    else:
        timing_window = "No active selling windows are open."

    where_we_are = f"""Freis Farm Weekly Brief:

WHERE WE ARE:
Corn left: {corn_left:,} bu.
{timing_window}
"""

    # --- WHAT THE MARKET IS DOING ---
    market_section = f"""WHAT THE MARKET IS DOING:
Futures: ${market_data['futures']:.4f}
Cash Bid: ~${cash_bid:.2f}
Oil: ${market_data['oil']:.2f}
Dollar Index: {market_data['dxy']:.2f}
"""

    # --- WHAT THIS MEANS ---
    # This section is more narrative and requires interpretation.
    # For now, we'll use a relatively static interpretation.
    what_this_means = """WHAT THIS MEANS:
This is a key period for old-crop corn. Market conditions are influenced by weather forecasts and global demand.
"""
    if market_data['oil'] < 75:
        what_this_means += "Soft oil prices may limit support from ethanol demand.\n"
    if market_data['dxy'] > 105:
        what_this_means += "A strong dollar can be a headwind for exports.\n"


    # --- WHAT I'D DO / WHY ---
    if next_tranche:
        sell_pct = next_tranche["pct"]
        sell_bu = int(corn_left * (sell_pct / 100))
        what_id_do = f"""WHAT I'D DO:
Plan to sell about {sell_pct}% of the remaining corn (~{sell_bu:,} bu) if price targets are met in the current window.

WHY:
This strategy locks in value while the seasonal window is open, keeps most of the crop in play for potential rallies, and avoids waiting too long and missing the seasonal peak.
"""
    else:
        what_id_do = """WHAT I'D DO:
Hold current position. No active selling tranches are open.

WHY:
We are waiting for the next planned seasonal window to open to re-evaluate selling opportunities.
"""
    
    # --- UPCOMING EVENTS ---
    if upcoming_events:
        events_section = "CALENDAR: \n" + "\n".join(upcoming_events)
    else:
        events_section = "CALENDAR:\nNo major reports on the calendar this week."


    return "\n".join([where_we_are, market_section, what_this_means, what_id_do, events_section])


def send_sms(message: str) -> bool:
    """Sends the message via TextBelt to the configured recipients."""
    if not TEXTBELT_KEY or not WEEKLY_REPORT_PHONES:
        print("ERROR: TEXTBELT_KEY or WEEKLY_REPORT_PHONES not set.")
        return False

    recipients = [p.strip() for p in WEEKLY_REPORT_PHONES.split(",")]
    all_ok = True
    for phone in recipients:
        try:
            response = httpx.post(
                "https://textbelt.com/text",
                {
                    "phone": phone,
                    "message": message,
                    "key": TEXTBELT_KEY,
                },
                timeout=15.0,
            )
            result = response.json()
            if not result.get("success"):
                print(f"ERROR sending to {phone}: {result.get('error')}")
                all_ok = False
        except Exception as e:
            print(f"ERROR sending to {phone}: {e}")
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    """Main function to generate and send the weekly brief."""
    print("Generating weekly brief...")

    try:
        plan = load_plan()
        corn_inventory = get_corn_inventory()
        market_data = get_market_data()
        cash_bid = get_cash_bid()

        next_tranche = find_next_tranche(plan)
        upcoming_events = find_upcoming_events(plan)

        brief_message = generate_brief(
            corn_inventory, next_tranche, market_data, cash_bid, upcoming_events
        )

        print("\n--- START of SMS message ---\n")
        print(brief_message)
        print("\n--- END of SMS message ---\n")

        if send_sms(brief_message):
            print("Successfully sent weekly brief to all recipients.")
        else:
            print("Failed to send weekly brief to one or more recipients.")

    except Exception as e:
        print(f"An error occurred: {e}")
        # Send a failure alert if something goes wrong
        send_sms(f"Freis Farm Alert: The weekly report failed to generate. Error: {e}")


if __name__ == "__main__":
    main()
