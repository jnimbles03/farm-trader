"""
Elevator-bid scraper — pulls regional cash bids from FS Grain's cashgrid.php
endpoint, which acts as an aggregator for ~36 Illinois elevators (their own
locations plus ADM, CGB, CHS, Cargill, etc.). Combines posted basis values
with live CME futures from agricharts.com to compute spot cash bids, then
filters to elevators within ~30 mi of the Freis farm and emits a JSON
sidecar consumed by farm-ops.html's "Where else this could go" panel.

Designed to run identically on a Mac, in a Cowork sandbox, and on a GitHub
Actions ubuntu-latest runner. Pure stdlib + requests. No auth required —
the FS Grain page is fully public.

Pipeline:
  1. GET https://www.fsgrain.com/markets/cashgrid.php
  2. Parse <option value="ID">Name</option> for the location id→name map.
  3. Parse writeBidCell(basis, ..., 'c=COMMODITY&l=LOCATION&d=MONTH', ...,
     quotes['SYMBOL']) calls — basis is in cents, symbol identifies the
     futures contract the basis is added to.
  4. GET the agricharts jsquote.php endpoint to resolve futures last-prices
     for ZCN26, ZCZ26, ZCH27, ZSN26, ZSX26, ZSF27 (the symbols the page
     references).
  5. For each curated nearby elevator, pick the spot-month bid (prefer
     current calendar month; fall back to next forward month with a bid).
  6. Write data/elevator_bids.json — schema matches what the panel expects.

Refreshing nightly is fine — futures move intraday but basis is set by the
elevator and only updates a couple times a day at most.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
FS_GRAIN_URL = "https://www.fsgrain.com/markets/cashgrid.php"
AGRICHARTS_URL = (
    "https://www.agricharts.com/marketdata/jsquote.php"
    "?varname=quotes&symbols={symbols}&fields=name,month,last"
    "&user=&pass=&settle=0&exchsyms=&currencyconv=&display_ice="
    "&ice_exchanges=&displayType=bids"
)
UA = {"User-Agent": "Mozilla/5.0 (compatible; freis-farm-bids/1.0)"}

# ---------------------------------------------------------------------------
# Curated set of nearby elevators. Names match what FS Grain uses verbatim.
# miles_approx is straight-line from 19076 W Peotone Rd, Wilmington, IL —
# refine to road distance once we wire OSRM.
# ---------------------------------------------------------------------------
NEARBY: list[dict] = [
    # name as published on FS Grain      display name                        miles
    {"fs_name": "Mazon",                 "display": "Mazon (FS Grain)",                  "miles": 10},
    {"fs_name": "CHS - Elburn/ Morris",  "display": "CHS Morris",                        "miles": 14},
    {"fs_name": "ADM - Morris",          "display": "ADM Morris (river)",                "miles": 14},
    {"fs_name": "Lisbon Center",         "display": "Lisbon Center (FS Grain)",          "miles": 15},
    {"fs_name": "CGB - Dwight",          "display": "CGB Dwight",                        "miles": 22},
    {"fs_name": "Ransom",                "display": "Ransom (FS Grain)",                 "miles": 22},
    {"fs_name": "Bourbonnais",           "display": "Bourbonnais (FS Grain / Heritage)", "miles": 22},
    {"fs_name": "Grand Ridge",           "display": "Grand Ridge (FS Grain)",            "miles": 28},
    {"fs_name": "Exline",                "display": "Exline (FS Grain)",                 "miles": 28},
    {"fs_name": "St Anne",               "display": "St Anne (FS Grain)",                "miles": 28},
]

FARM_ANCHOR = "19076 W Peotone Rd, Wilmington, IL 60481"
FREIGHT_PER_MILE_CENTS = 0.6  # $5/loaded mi ÷ ~890 bu/load corn

# Path discovery — works whether the script sits at project root or inside
# farm-proxy/ (CI checkout). We look for a sibling/child docs/ directory
# containing bushel.json (Ritchie's bid feed).
HERE = Path(__file__).resolve().parent


def _find_docs_dir() -> Optional[Path]:
    """Locate the farm-proxy/docs directory regardless of where this script lives."""
    for candidate in (
        HERE / "farm-proxy" / "docs",   # script at project root
        HERE / "docs",                  # script inside farm-proxy/
        HERE.parent / "docs",           # script in farm-proxy/scripts/
        HERE.parent / "farm-proxy" / "docs",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _find_data_dir() -> Optional[Path]:
    """Locate the existing data/ directory at the project root, if any.

    Unlike _find_docs_dir, this never creates a directory — the developer
    JSON copy is purely a convenience for local hacking and shouldn't be
    forced into existence in CI.
    """
    for candidate in (
        HERE / "data",                  # script at project root
        HERE.parent / "data",           # script inside farm-proxy/
        HERE.parent.parent / "data",    # script in farm-proxy/scripts/
    ):
        if candidate.is_dir():
            return candidate.resolve()
    return None

# ---------------------------------------------------------------------------
# FS Grain page: extract location id→name map and the writeBidCell records.
# ---------------------------------------------------------------------------
_OPTION_RE = re.compile(r'<option\s+value="(\d+)">([^<]+)</option>')
_WRITEBID_RE = re.compile(r"writeBidCell\(\s*([^)]*?)\s*\)", re.S)
_QUOTE_REF_RE = re.compile(r"quotes\['([A-Z]+\d+)'\]")


def _split_args(s: str) -> list[str]:
    """Split JS arg list on commas not inside single quotes."""
    return [p.strip() for p in re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", s)]


def fetch_fs_grain_html(session: requests.Session) -> str:
    r = session.get(FS_GRAIN_URL, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text


def parse_locations(html: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _OPTION_RE.finditer(html)}


def parse_bid_cells(html: str, locs: dict[str, str]) -> list[dict]:
    """Return list of {loc_name, c, loc_id, month_code, basis_cents, futures_symbol}."""
    out: list[dict] = []
    for m in _WRITEBID_RE.finditer(html):
        parts = _split_args(m.group(1))
        if len(parts) < 8:
            continue
        ident = parts[5].strip("'")
        sm = _QUOTE_REF_RE.search(parts[7])
        if not sm:
            continue
        try:
            basis = float(parts[0])
        except ValueError:
            basis = None
        if basis is None:
            continue
        try:
            pd = dict(p.split("=") for p in ident.split("&"))
        except Exception:
            continue
        loc_id = pd.get("l")
        if loc_id is None:
            continue
        out.append(
            {
                "loc_id": loc_id,
                "loc_name": locs.get(loc_id, "?"),
                "c": pd.get("c"),
                "month_code": pd.get("d"),
                "basis_cents": basis,
                "futures_symbol": sm.group(1),
            }
        )
    return out


# ---------------------------------------------------------------------------
# AgriCharts: fetch futures last-prices.
# ---------------------------------------------------------------------------
def fetch_futures_prices(
    session: requests.Session, symbols: list[str]
) -> dict[str, float]:
    """Return {symbol: last_price_in_cents} for the requested symbols.

    AgriCharts returns a JS blob `var quotes = { 'ZCN26': { last: '479.75', ... }, ... };`
    We parse it with regex (no eval — refuse to execute remote JS).
    """
    url = AGRICHARTS_URL.format(symbols=",".join(symbols))
    r = session.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    text = r.text

    out: dict[str, float] = {}
    # Each symbol's block ends with `}` before the next quote symbol or `};`
    # Capture: 'SYMBOL': { ... last: 'NNN.NN' ... }
    block_re = re.compile(
        r"'([A-Z]+\d+)'\s*:\s*\{(?P<body>[^{}]*)\}", re.S
    )
    for m in block_re.finditer(text):
        sym = m.group(1)
        body = m.group("body")
        lm = re.search(r"\blast\s*:\s*'([\d.\-]+)'", body)
        if lm:
            try:
                out[sym] = float(lm.group(1))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
# Bid resolution: pick the spot/front-month bid per (location, commodity).
# Preference order: current calendar month code → next available forward.
# ---------------------------------------------------------------------------
_MONTH_CODES = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]


def current_month_code(today: Optional[dt.date] = None) -> str:
    today = today or dt.date.today()
    return _MONTH_CODES[today.month - 1]


def pick_spot_bid(
    cells: list[dict], commodity_id: str, today: Optional[dt.date] = None
) -> Optional[dict]:
    """Pick the bid cell representing the front-month delivery for this commodity.

    Strategy: among cells matching the commodity, prefer the one whose month
    letter matches the current calendar month; otherwise the chronologically
    nearest forward month.
    """
    today = today or dt.date.today()
    rows = [c for c in cells if c["c"] == commodity_id]
    if not rows:
        return None

    cur_letter = current_month_code(today)
    cur_year_2digit = today.year % 100

    def sort_key(r):
        mc = r["month_code"]  # e.g. "K26"
        letter = mc[0]
        try:
            year = 2000 + int(mc[1:])
        except ValueError:
            return (9999, 99)
        idx = _MONTH_CODES.index(letter) if letter in _MONTH_CODES else 12
        # months_from_now: positive if future, large positive if past
        months_from_today = (year - today.year) * 12 + (idx + 1 - today.month)
        if months_from_today < 0:
            months_from_today += 1000  # push past months to the back
        return (months_from_today, mc)

    rows.sort(key=sort_key)
    return rows[0]


def cents_to_dollars(c: float) -> float:
    return round(c / 100.0, 4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
COMMODITY_CORN = "17336"
COMMODITY_SOY = "17337"
EXPECTED_FUTURES = ["ZCN26", "ZCZ26", "ZCH27", "ZSN26", "ZSX26", "ZSF27"]


def load_ritchie_baseline(path: Optional[Path] = None) -> Optional[dict]:
    """Pull Ritchie's spot corn + soy bid from the Bushel scraper's output.

    Returns an elevator dict (same shape as FS Grain rows) or None if the
    bushel.json file isn't available.
    """
    if path is None:
        docs = _find_docs_dir()
        if docs is None:
            return None
        path = docs / "bushel.json"
    if not path.exists():
        return None
    try:
        b = json.loads(path.read_text())
    except Exception:
        return None

    bids = b.get("bids", {}) or {}
    corn = bids.get("corn") or {}
    soy = bids.get("soybeans") or {}

    return {
        "id": "ritchie",
        "name": "Ritchie Grain",
        "fs_name": None,
        "address": "34511 Elevator Rd, Wilmington (Akron Services)",
        "miles": 5,
        "corn_bid": corn.get("price"),
        "soy_bid": soy.get("price"),
        "corn_basis_cents": (corn.get("basis") * 100) if corn.get("basis") is not None else None,
        "soy_basis_cents":  (soy.get("basis")  * 100) if soy.get("basis")  is not None else None,
        "corn_contract": corn.get("period"),
        "soy_contract": soy.get("period"),
        "corn_futures": corn.get("futuresSymbol"),
        "soy_futures":  soy.get("futuresSymbol"),
        "source": "bushel:akronservices",
        "scraper_status": "wired",
        "is_baseline": True,
        "as_of": b.get("fetchedAt"),
    }


def build_payload(
    cells: list[dict],
    futures: dict[str, float],
    today: Optional[dt.date] = None,
    ritchie_row: Optional[dict] = None,
) -> dict:
    today = today or dt.date.today()
    cells_by_loc: dict[str, list[dict]] = {}
    for c in cells:
        cells_by_loc.setdefault(c["loc_name"], []).append(c)

    elevators_out: list[dict] = []
    if ritchie_row is not None:
        elevators_out.append(ritchie_row)
    missing: list[str] = []
    for entry in NEARBY:
        loc_cells = cells_by_loc.get(entry["fs_name"], [])
        if not loc_cells:
            missing.append(entry["fs_name"])

        corn_pick = pick_spot_bid(loc_cells, COMMODITY_CORN, today)
        soy_pick = pick_spot_bid(loc_cells, COMMODITY_SOY, today)

        def resolve_bid(pick):
            if not pick:
                return None, None
            fut_price = futures.get(pick["futures_symbol"])
            if fut_price is None:
                return None, pick
            cash_cents = fut_price + pick["basis_cents"]
            return cents_to_dollars(cash_cents), pick

        corn_bid, corn_meta = resolve_bid(corn_pick)
        soy_bid, soy_meta = resolve_bid(soy_pick)

        elevators_out.append(
            {
                "id": entry["fs_name"].lower().replace(" ", "_").replace("-", "_").replace("/", "_"),
                "name": entry["display"],
                "fs_name": entry["fs_name"],
                "miles": entry["miles"],
                "corn_bid": corn_bid,
                "soy_bid": soy_bid,
                "corn_basis_cents": corn_pick["basis_cents"] if corn_pick else None,
                "soy_basis_cents": soy_pick["basis_cents"] if soy_pick else None,
                "corn_contract": corn_pick["month_code"] if corn_pick else None,
                "soy_contract": soy_pick["month_code"] if soy_pick else None,
                "corn_futures": corn_pick["futures_symbol"] if corn_pick else None,
                "soy_futures": soy_pick["futures_symbol"] if soy_pick else None,
                "source": "fsgrain.com/markets/cashgrid.php",
                "scraper_status": "live" if (corn_bid or soy_bid) else "no-data",
                "is_baseline": False,
            }
        )

    payload = {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "freight_per_mile_cents_per_bu": FREIGHT_PER_MILE_CENTS,
        "freight_assumption_text": "$5/loaded mi ÷ ~890 bu/load corn ≈ 0.6¢/bu per extra mile",
        "farm_anchor_address": FARM_ANCHOR,
        "baseline_id": "ritchie",
        "futures": {sym: futures.get(sym) for sym in EXPECTED_FUTURES},
        "elevators": elevators_out,
        "missing_from_source": missing,
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    _data = _find_data_dir()
    ap.add_argument(
        "--out",
        default=str(_data / "elevator_bids.json") if _data else None,
        help="Optional dev-copy of the JSON sidecar. Defaults to "
             "data/elevator_bids.json if a project-root data/ dir exists; "
             "skipped otherwise. The deploy copy at farm-proxy/docs/elevator_bids.json "
             "is always written when discoverable.",
    )
    ap.add_argument(
        "--cached-html",
        help="Use a local HTML file instead of fetching live (for tests).",
    )
    ap.add_argument(
        "--cached-quotes",
        help="Use a local JS quotes file instead of fetching live (for tests).",
    )
    args = ap.parse_args(argv)

    session = requests.Session()

    if args.cached_html:
        html = Path(args.cached_html).read_text()
    else:
        html = fetch_fs_grain_html(session)

    locs = parse_locations(html)
    cells = parse_bid_cells(html, locs)

    futures_symbols = sorted({c["futures_symbol"] for c in cells})
    if not futures_symbols:
        sys.stderr.write("!! no bid cells parsed from FS Grain page\n")
        return 3

    if args.cached_quotes:
        # The cached quotes file is the same JS blob format
        text = Path(args.cached_quotes).read_text()
        # reuse fetch_futures_prices' parser by writing to a stub session
        class _Stub:
            def get(self, *a, **k):
                class R:
                    pass
                r = R()
                r.text = text
                r.raise_for_status = lambda: None
                return r
        futures = fetch_futures_prices(_Stub(), futures_symbols)  # type: ignore
    else:
        futures = fetch_futures_prices(session, futures_symbols)

    ritchie = load_ritchie_baseline()
    if ritchie is None:
        sys.stderr.write(
            f"!! Ritchie baseline unavailable ({RITCHIE_BUSHEL_JSON} not found).\n"
            "   Panel will fall back to showing basis cents in the Δ columns.\n"
        )

    payload = build_payload(cells, futures, ritchie_row=ritchie)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Wrote {out_path}")

    # Also write a copy alongside the deployed HTML so the panel can fetch it
    # over HTTP when served from GitHub Pages / Cloudflare. Optional — silent
    # if the docs/ directory doesn't exist.
    docs_dir = _find_docs_dir()
    if docs_dir is not None:
        docs_path = docs_dir / "elevator_bids.json"
        docs_path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Wrote {docs_path}")
    else:
        print("Skipped docs/ copy — no farm-proxy/docs directory found.")

    print(f"Wrote {out_path}")
    print(
        f"  {len(payload['elevators'])} elevators, "
        f"{sum(1 for e in payload['elevators'] if e['corn_bid'])} with corn bids, "
        f"{sum(1 for e in payload['elevators'] if e['soy_bid'])} with soy bids"
    )
    if payload["missing_from_source"]:
        print(f"  missing from source: {payload['missing_from_source']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
