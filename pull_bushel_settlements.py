"""
Pull Bushel settlements + scale tickets to CSV.

Two modes:
  1. Live  — uses a Bearer JWT to call centre.bushelops.com directly.
              Paginates POST /api/v2/settlements?page=N and then for each row
              calls GET /api/v1/settlements/{id} to pull the embedded scale
              tickets, payment adjustments, and quality remarks.

  2. HAR   — replays from a captured Proxyman HAR so we can build sample
              CSVs without a live token. Same parser walks the response
              bodies (which Proxyman base64-encodes when the original
              transport was gzip).

Outputs (under farm_ops_data/):
  settlements.csv      one row per settlement (the priced sale)
  tickets.csv          one row per scale ticket (the truck-load weigh-in)
  adjustments.csv      one row per payment adjustment (checkoff, storage, etc.)
  remarks.csv          one row per quality remark on a ticket (moisture, dock, ...)
  summary_year_crop.csv  rollup by year × crop

Usage:
  # live, with a JWT pasted from a logged-in browser
  python pull_bushel_settlements.py --token "$BUSHEL_JWT"

  # offline, replay from the captured HAR
  python pull_bushel_settlements.py --har akron_recon/bushel3/centre.bushelops.com_04-28-2026-17-57-58.har

Why HAR mode exists:
  Bushel's JWT expires (~5 min) and refreshing it requires the Keycloak
  flow which we haven't automated yet. The HAR replay lets the Farm Ops
  dashboard render real numbers today; once we automate the token mint
  we just stop passing --har.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests

CENTRE = "https://centre.bushelops.com"
DEFAULT_OUT = Path(__file__).parent / "farm_ops_data"


# ───────────────────────────────────────── HTTP / decode helpers ──

def _decode_body(text: str) -> Any:
    """Bushel responses through Proxyman come back base64-wrapped (because
    the original transport was gzip + Proxyman re-encodes binary). Live
    requests come back as plain JSON. Try both."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return json.loads(base64.b64decode(text))
    except Exception:
        return None


def _hdrs(token: str, installation_id: str | None = None,
          company: str = "akronservices", app_version: str = "0.8.84") -> dict:
    """Headers centre.bushelops.com requires.

    Without `app-company` + `app-installation-id` the API responds 401/500
    even with a valid Bearer — the JWT alone isn't tenant-scoped.
    `installation_id` is per-portal-session; pull it from
    __NEXT_DATA__.props.installationId after login.
    """
    h = {
        "Authorization":  f"Bearer {token}",
        "Accept":         "application/json",
        "Content-Type":   "application/json",
        "Origin":         "https://portal.bushelpowered.com",
        "Referer":        "https://portal.bushelpowered.com/",
        "app-company":    company,
        "app-name":       "bushel-web-portal-prod",
        "app-version":    app_version,
    }
    if installation_id:
        h["app-installation-id"] = installation_id
    return h


# ─────────────────────────────────────────────────── live fetch ──

def fetch_settlements_live(token: str, installation_id: str | None = None) -> tuple[list[dict], dict[int, dict]]:
    """Returns (list of settlement summaries, {id: full detail})."""
    s = requests.Session()
    s.headers.update(_hdrs(token, installation_id))

    summaries: list[dict] = []
    page = 1
    while True:
        r = s.post(f"{CENTRE}/api/v2/settlements?page={page}",
                   json={"filter": {}}, timeout=30)
        r.raise_for_status()
        body = r.json()
        rows = body.get("data", []) or []
        summaries.extend(rows)
        meta = body.get("meta", {}).get("pagination", {})
        total_pages = meta.get("total_pages", 1)
        print(f"  page {page}/{total_pages}: {len(rows)} settlements")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)  # be polite

    details: dict[int, dict] = {}
    for i, row in enumerate(summaries, 1):
        sid = row["id"]
        r = s.get(f"{CENTRE}/api/v1/settlements/{sid}", timeout=30)
        r.raise_for_status()
        details[sid] = r.json().get("data", {})
        if i % 10 == 0 or i == len(summaries):
            print(f"  detail {i}/{len(summaries)}")
        time.sleep(0.2)

    return summaries, details


# ─────────────────────────────────────────────────── HAR replay ──

def fetch_settlements_har(har_path: Path) -> tuple[list[dict], dict[int, dict]]:
    """Walk a captured HAR and pull the same shapes the live calls return."""
    har = json.loads(har_path.read_text())
    summaries: list[dict] = []
    details: dict[int, dict] = {}
    for entry in har["log"]["entries"]:
        req = entry["request"]
        if req["method"] == "OPTIONS":
            continue
        url = req["url"]
        body = _decode_body(entry["response"]["content"].get("text", ""))
        if body is None:
            continue
        if "/api/v2/settlements" in url and req["method"] == "POST":
            for row in body.get("data", []) or []:
                summaries.append(row)
        elif "/api/v1/settlements/" in url and req["method"] == "GET":
            d = body.get("data", {}) or {}
            if "id" in d:
                details[d["id"]] = d
    # Dedupe summaries by id (HAR may have repeats)
    seen, uniq = set(), []
    for s in summaries:
        if s["id"] in seen:
            continue
        seen.add(s["id"]); uniq.append(s)
    return uniq, details


# ──────────────────────────────────────────────────── CSV writers ──

def _money(s: Any) -> float | None:
    """Bushel returns money as both '$5,418.55' and 5418.55 depending on
    field. Coerce to float; preserve sign for parenthesized negatives."""
    if s is None: return None
    if isinstance(s, (int, float)): return float(s)
    s = str(s).strip().replace("USD", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()$ ").replace(",", "")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def _qty(s: Any) -> float | None:
    if s is None: return None
    if isinstance(s, (int, float)): return float(s)
    return _money(str(s).split()[0]) if s else None  # "500 bu" → 500.0


def write_csvs(summaries: list[dict], details: dict[int, dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    # settlements.csv
    s_rows = []
    for s in summaries:
        d = details.get(s["id"], {})
        s_rows.append({
            "settlement_id":          s["id"],
            "display_id":             s["display_id"],
            "remote_id":              d.get("remote_id"),
            "account_name":           (d.get("account") or {}).get("name") or s.get("remote_user_id"),
            "account_id":             (d.get("account") or {}).get("id"),
            "commodity":              s["commodity_name"],
            "settled_on_date":        s["settled_on_date"],
            "settled_qty_bu":         _qty(s.get("settled_quantity")),
            "gross_payment":          _money(d.get("gross_payment", s.get("gross_payment"))),
            "total_adjustment":       _money(d.get("total_adjustment")),
            "net_payment":            _money(s["net_payment"]),
            "amount_due":             _money(s.get("amount_due")),
            "payment_status":         s.get("translated_payment_status"),
            "check_numbers":          ";".join(d.get("check_numbers") or []),
            "payouts":                "; ".join(
                f"{p.get('display_id')}:{p.get('type')}:{_money(p.get('amount'))}"
                for p in (d.get("payouts") or [])
            ),
            "comments":               d.get("comments") or "",
        })
    _dump_csv(out / "settlements.csv", s_rows)

    # tickets.csv
    t_rows = []
    for sid, d in details.items():
        for t in d.get("tickets") or []:
            t_rows.append({
                "settlement_id":      sid,
                "settlement_display_id": d.get("display_id"),
                "commodity":          d.get("commodity_name"),
                "ticket_id":          t.get("id"),
                "ticket_remote_id":   t.get("remote_id"),
                "created_at":         t.get("created_at"),
                "location":           t.get("location_name"),
                "net_amount":         _qty(t.get("net_amount")),
                "uom":                t.get("crop_amount_uom"),
            })
    _dump_csv(out / "tickets.csv", t_rows)

    # adjustments.csv  ← THE input-cost-relevant rows: storage, checkoff, drying, etc.
    a_rows = []
    for sid, d in details.items():
        for adj in d.get("payment_adjustments") or []:
            a_rows.append({
                "settlement_id":      sid,
                "settlement_display_id": d.get("display_id"),
                "settled_on_date":    d.get("settled_on_date"),
                "commodity":          d.get("commodity_name"),
                "reason":             adj.get("reason"),
                "amount":             _money(adj.get("amount")),
            })
    _dump_csv(out / "adjustments.csv", a_rows)

    # remarks.csv
    r_rows = []
    for sid, d in details.items():
        for t in d.get("tickets") or []:
            for rm in t.get("remarks") or []:
                r_rows.append({
                    "settlement_id":      sid,
                    "ticket_id":          t.get("id"),
                    "ticket_remote_id":   t.get("remote_id"),
                    "type":               rm.get("type"),
                    "name":               rm.get("name"),
                    "value":              rm.get("value"),
                    "dock_unit":          rm.get("dock_unit"),
                    "dock_value":         rm.get("dock_value"),
                })
    _dump_csv(out / "remarks.csv", r_rows)

    # summary_year_crop.csv — rollup
    g: dict[tuple, dict] = defaultdict(lambda: {
        "n_settlements": 0, "total_bu": 0.0, "gross": 0.0, "adjustments": 0.0, "net": 0.0,
    })
    for r in s_rows:
        year = (r["settled_on_date"] or "????")[:4]
        key = (year, r["commodity"])
        g[key]["n_settlements"] += 1
        g[key]["total_bu"]      += r["settled_qty_bu"] or 0
        g[key]["gross"]         += r["gross_payment"] or 0
        g[key]["adjustments"]   += r["total_adjustment"] or 0
        g[key]["net"]           += r["net_payment"] or 0
    sum_rows = [
        {"year": y, "commodity": c, **vals,
         "avg_price_per_bu": (vals["gross"] / vals["total_bu"]) if vals["total_bu"] else None}
        for (y, c), vals in sorted(g.items())
    ]
    _dump_csv(out / "summary_year_crop.csv", sum_rows)

    # adjustments_summary.csv — what fees Ritchie has charged us, by reason
    g2: dict[str, dict] = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for r in a_rows:
        g2[r["reason"]]["count"]  += 1
        g2[r["reason"]]["amount"] += r["amount"] or 0
    adj_rows = [
        {"reason": k, **v} for k, v in sorted(
            g2.items(), key=lambda kv: kv[1]["amount"]
        )
    ]
    _dump_csv(out / "adjustments_summary.csv", adj_rows)

    print(f"\n  wrote {len(s_rows)} settlements, {len(t_rows)} tickets, "
          f"{len(a_rows)} adjustments, {len(r_rows)} remarks → {out}/")


def _dump_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ────────────────────────────────────────────────────────── main ──

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--token", help="Bushel JWT (Bearer ...). Live mode.")
    src.add_argument("--har", type=Path, help="Path to centre.bushelops.com HAR. Replay mode.")
    ap.add_argument("--installation-id", default=None,
                    help="Portal session installationId — required for live mode "
                         "(centre.bushelops.com 401s without it). Get it from "
                         "__NEXT_DATA__.props.installationId after a portal login.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"Output dir (default {DEFAULT_OUT})")
    args = ap.parse_args()

    if args.token:
        print("[live] paginating settlements...")
        summaries, details = fetch_settlements_live(args.token, args.installation_id)
    else:
        if not args.har.exists():
            print(f"!! HAR not found: {args.har}", file=sys.stderr); return 2
        print(f"[har] parsing {args.har.name}")
        summaries, details = fetch_settlements_har(args.har)
        print(f"      {len(summaries)} settlements, {len(details)} with detail")

    if not summaries:
        print("!! no settlements parsed; nothing to write", file=sys.stderr)
        return 1

    write_csvs(summaries, details, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
