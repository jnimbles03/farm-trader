#!/usr/bin/env python3
"""
build_sales_log_from_bushel.py
==============================

Generate `farm-proxy/docs/sales_log.json` directly from Bushel data.

Architecture:

  scripts/scrape_bushel.py
    └── writes docs/bushel_raw.json   (every 15 min via refresh-bushel.yml)
                  │
                  ▼
  scripts/build_sales_log_from_bushel.py
    └── reads  docs/bushel_raw.json
        + scripts/sales_log_static.json   (Posen Coop backfill — Bushel can't see PC)
        + docs/advisor/ritchie_live.json  (current storage for the dashboard footer)
    └── writes docs/sales_log.json

Akron / Ritchie sales come from `bushel_raw.contracts.data`, filtered to
closed, priced, purchase-type rows whose first delivery period falls on
or after the current marketing year start (Sep 1).

Posen Coop sales never hit Bushel (different elevator), so they're
hand-maintained in `scripts/sales_log_static.json`. Add a row there when
you deliver to PC; everything else is automatic.

Run as a step inside refresh-bushel.yml after the scrape, or locally:

    python3 farm-proxy/scripts/build_sales_log_from_bushel.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # farm-proxy/
DOCS = ROOT / "docs"
RAW = DOCS / "bushel_raw.json"
LIVE = DOCS / "advisor" / "ritchie_live.json"
STATIC = HERE / "sales_log_static.json"
OUT = DOCS / "sales_log.json"

PRICE_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


# ---------------------------------------------------------------------------
# Marketing-year + parsing helpers
# ---------------------------------------------------------------------------


def marketing_year_window(today: date) -> tuple[date, str]:
    """MY = Sep 1 of year N → Aug 31 of N+1. Today picks the containing window."""
    start_year = today.year if today.month >= 9 else today.year - 1
    return date(start_year, 9, 1), f"{start_year}-{start_year + 1}"


def parse_price(status: str | None) -> float | None:
    if not status:
        return None
    m = PRICE_RE.search(status.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_first_date(period: str | None) -> str | None:
    """Pull the first MM/DD/YYYY out of '11/01/2025 - 11/30/2025'."""
    if not period:
        return None
    m = DATE_RE.search(period)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        return date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def normalize_crop(name: str | None) -> str:
    n = (name or "").strip().lower()
    if n.startswith("soy"):
        return "soybeans"
    if n == "corn":
        return "corn"
    return n


def first_delivery_period(contract: dict) -> tuple[str | None, str | None]:
    """Return (first_delivery_date_iso, location_name)."""
    schedule = contract.get("deliverySchedule") or []
    if not schedule:
        return None, None
    first = schedule[0]
    periods = first.get("deliveryPeriods") or []
    if not periods:
        return None, first.get("locationName")
    return parse_first_date(periods[0]), first.get("locationName")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def extract_bushel_sales(raw: dict, my_start: date) -> list[dict]:
    """Pull closed, priced, purchase-type sales from Bushel contracts."""
    contracts = ((raw.get("contracts") or {}).get("data")) or []
    out: list[dict] = []
    for c in contracts:
        # Closed only — open contracts are unfulfilled commitments.
        if not c.get("isClosed"):
            continue
        # Purchase-type from the elevator's perspective = a sale by us.
        if (c.get("contractType") or "").lower() != "purchase":
            continue
        # Skip cancelled / never-delivered tickets.
        delivered = float(c.get("quantityDelivered") or 0)
        if delivered <= 0:
            continue
        price = parse_price(c.get("pricingStatus"))
        if price is None:
            continue
        sale_date, location = first_delivery_period(c)
        if not sale_date or sale_date < my_start.isoformat():
            continue

        out.append(
            {
                "date": sale_date,
                "crop": normalize_crop(c.get("commodityName")),
                "buyer": "Akron / Ritchie",
                "bushels": round(delivered, 2),
                "price_per_bu": round(price, 4),
                "total_amount": round(delivered * price, 2),
                "ticket_id": c.get("displayId"),
                "contract_type": c.get("displayType"),
                "location": location,
                "source": "bushel",
            }
        )
    return out


def load_static_sales(my_start: date) -> list[dict]:
    """Read hand-maintained sales (Posen Coop, etc.) from sales_log_static.json."""
    if not STATIC.exists():
        return []
    try:
        data = json.loads(STATIC.read_text())
    except Exception as e:
        sys.stderr.write(f"warn: couldn't parse {STATIC}: {e}\n")
        return []
    rows = data.get("sales") or []
    out: list[dict] = []
    for r in rows:
        d = r.get("date") or ""
        if d < my_start.isoformat():
            continue
        bushels = float(r.get("bushels") or 0)
        price = r.get("price_per_bu")
        total = r.get("total_amount")
        if total is None and price is not None and bushels:
            total = round(bushels * float(price), 2)
        out.append(
            {
                "date": d,
                "crop": normalize_crop(r.get("crop")),
                "buyer": r.get("buyer") or "—",
                "bushels": round(bushels, 2),
                "price_per_bu": round(float(price), 4) if price is not None else None,
                "total_amount": round(float(total), 2) if total is not None else None,
                "ticket_id": r.get("ticket_id"),
                "contract_type": r.get("contract_type"),
                "location": r.get("location"),
                "source": "static",
            }
        )
    return out


def load_live_storage() -> dict | None:
    if not LIVE.exists():
        return None
    try:
        live = json.loads(LIVE.read_text())
    except Exception as e:
        sys.stderr.write(f"warn: couldn't parse {LIVE}: {e}\n")
        return None
    storage = live.get("storage") or {}
    return {
        "corn_bu": storage.get("corn_bu"),
        "beans_bu": storage.get("beans_bu"),
        "as_of": (live.get("as_of") or "")[:10] or None,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def season_totals(sales: list[dict]) -> dict:
    out = {
        "soybeans": {"booked_bushels": 0.0, "booked_amount": 0.0, "sales_count": 0},
        "corn":     {"booked_bushels": 0.0, "booked_amount": 0.0, "sales_count": 0},
        "combined": {"booked_bushels": 0.0, "booked_amount": 0.0},
    }
    for s in sales:
        crop = s.get("crop")
        if crop not in ("soybeans", "corn"):
            continue
        bu = float(s.get("bushels") or 0)
        amt = float(s.get("total_amount") or 0)
        out[crop]["booked_bushels"] += bu
        out[crop]["booked_amount"] += amt
        out[crop]["sales_count"] += 1
        out["combined"]["booked_bushels"] += bu
        out["combined"]["booked_amount"] += amt

    for crop in ("soybeans", "corn"):
        bu = out[crop]["booked_bushels"]
        amt = out[crop]["booked_amount"]
        out[crop]["booked_bushels"] = round(bu, 2)
        out[crop]["booked_amount"] = round(amt, 2)
        out[crop]["weighted_avg_price"] = round(amt / bu, 4) if bu else None
    out["combined"]["booked_bushels"] = round(out["combined"]["booked_bushels"], 2)
    out["combined"]["booked_amount"] = round(out["combined"]["booked_amount"], 2)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if not RAW.exists():
        sys.stderr.write(f"error: {RAW} missing — run scrape_bushel.py first\n")
        return 1
    raw = json.loads(RAW.read_text())

    today = datetime.utcnow().date()
    my_start, my_label = marketing_year_window(today)

    bushel_sales = extract_bushel_sales(raw, my_start)
    static_sales = load_static_sales(my_start)

    sales = sorted(
        bushel_sales + static_sales,
        key=lambda s: (s.get("date") or "", s.get("buyer") or ""),
    )
    totals = season_totals(sales)
    storage = load_live_storage()

    bundle = {
        "marketing_year": my_label,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "data_source": {
            "akron_ritchie": "docs/bushel_raw.json :: contracts.data (closed, priced, purchase-type)",
            "posen_coop":    "scripts/sales_log_static.json (hand-maintained; Bushel doesn't see PC)",
            "remaining":     "docs/advisor/ritchie_live.json :: storage",
        },
        "sales": sales,
        "pending": [],
        "season_totals": totals,
        "remaining_at_ritchie": storage,
        "notes": [
            f"{len(bushel_sales)} Akron/Ritchie sales pulled live from Bushel; "
            f"{len(static_sales)} Posen Coop sales from static backfill.",
            "Sales appear here as soon as Bushel records them — no books-xlsx step in the loop.",
            "To add a non-Bushel sale (Posen Coop, hand-sale, etc.), edit "
            "scripts/sales_log_static.json and commit.",
        ],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(bundle, indent=2) + "\n")
    print(
        f"wrote {OUT.relative_to(ROOT)} :: MY {my_label} :: "
        f"{len(sales)} sales ({len(bushel_sales)} Bushel + {len(static_sales)} static)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
