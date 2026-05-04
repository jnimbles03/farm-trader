"""
refresh_ritchie.py — single entrypoint that pulls live Ritchie data from
Bushel and writes advisor/ritchie_live.json. Designed to run on a GHA
runner before advisor/context_builder.py.

Pipeline (all pure HTTP, no browser):
  1. Portal-mediated Bushel login (see scrape_bushel_bids.get_token_portal).
  2. Extract installationId from /akronservices/auth/check?post_login=1.
  3. GET centre.bushelops.com/api/v2/commodity-balances
        → corn / soy bushels at Ritchie Grain Facility.
  4. POST api.bushelpowered.com/.../GetBidsList
        → live cash/basis/futures by month for corn + soy.
  5. (Best-effort) POST .../grain/v1/GetAllContracts to surface Average
     Pricing Program enrollment. Skipped silently on failure — it's a nice
     to have, not a blocker.
  6. Write advisor/ritchie_live.json with a stable schema the
     context_builder + advisor persona can read.

Inputs (env or .env):
  BUSHEL_USER  — phone-derived digit string (e.g. 6302122546)
  BUSHEL_PASS

Output schema (advisor/ritchie_live.json):
  {
    "as_of":              "2026-05-04T18:32:11Z",
    "source":             "bushel.commodity-balances + bids",
    "account":            "FREIS FARMS LLC",
    "location":           "Ritchie Grain Facility",
    "storage": {
      "corn_bu":   9115.28,
      "beans_bu":  1967.12
    },
    "avg_pricing": {
      "corn_bu":   1500,    // null if we can't derive
      "soy_bu":    500
    },
    "bids":               { ...same shape scrape_bushel_bids emits... },
    "fetch_errors":       []   // non-fatal warnings collected during the run
  }

Exit codes:
  0  full success
  2  auth failure (refuses to write a partial sidecar)
  3  storage balances missing (auth ok but balance endpoint blank)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # GHA runner: env comes from job secrets

# Reuse the auth + bid-fetch code we already proved works.
from scrape_bushel_bids import (
    USER_AGENT,
    PORTAL_AUTH_CHECK,
    COMPANY_SLUG,
    RITCHIE_LOCATION_NAME,
    get_token_ropc,
    get_token_portal,
    fetch_bids_raw,
    shape_ritchie,
)

CENTRE = "https://centre.bushelops.com"
API_BASE = "https://api.bushelpowered.com"
CONTRACTS_URL = f"{API_BASE}/api/aggregator/grain/v1/GetAllContracts"

# Default sidecar location: docs/advisor/ so GitHub Pages serves it
# (the Worker fetches from that origin). The xlsx-driven advisor_context.json
# also lives there; this is the small companion file with live Ritchie state.
OUT_PATH = Path(__file__).parent / "docs" / "advisor" / "ritchie_live.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _centre_headers(token: str, installation_id: str, app_version: str) -> dict:
    return {
        "Authorization":       f"Bearer {token}",
        "Accept":              "application/json",
        "Content-Type":        "application/json",
        "Origin":              "https://portal.bushelpowered.com",
        "Referer":             "https://portal.bushelpowered.com/",
        "app-company":         COMPANY_SLUG,
        "app-installation-id": installation_id,
        "app-name":            "bushel-web-portal-prod",
        "app-version":         app_version,
    }


def _api_headers(token: str) -> dict:
    """api.bushelpowered.com only needs app-company; centre's app-installation-id
    is not required here based on the captured HARs."""
    return {
        "Authorization":  f"Bearer {token}",
        "Accept":         "*/*",
        "Content-Type":   "application/json",
        "app-company":    COMPANY_SLUG,
        "Origin":         "https://portal.bushelpowered.com",
        "Referer":        "https://portal.bushelpowered.com/",
    }


def fetch_session_meta(session: requests.Session) -> dict:
    """After login the portal cookies are set; auth/check returns
    __NEXT_DATA__ with installationId + version we need for centre.bushelops."""
    r = session.get(PORTAL_AUTH_CHECK, timeout=20)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("auth/check returned page without __NEXT_DATA__")
    nd = json.loads(m.group(1))
    props = nd.get("props") or {}
    return {
        "installation_id": props.get("installationId"),
        "version":         props.get("version") or "0.8.84",
        "tenant_id":       props.get("tenantId"),
        "user":            props.get("user"),
    }


def fetch_commodity_balances(token: str, installation_id: str, version: str) -> dict:
    """Returns the parsed balances payload from centre.bushelops.com.

    Shape (from captured HAR):
      {"data": [
        {"crop_name": "Corn",     "total_numeric": 9115.28,
         "location_totals": [{"location_name": "Ritchie Grain Facility", ...}]},
        {"crop_name": "Soybeans", "total_numeric": 1967.12, ...}
      ], "meta": {"last_updated": null}}
    """
    r = requests.get(
        f"{CENTRE}/api/v2/commodity-balances",
        headers=_centre_headers(token, installation_id, version),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def shape_storage(balances: dict) -> tuple[dict[str, float | None], str | None]:
    """Pick out (corn_bu, beans_bu) at Ritchie from the balances payload.

    Returns ({"corn_bu": 9115.28, "beans_bu": 1967.12}, account_name).
    Missing crops resolve to None rather than 0 — distinguishes "no entry"
    from a real zero balance.
    """
    out: dict[str, float | None] = {"corn_bu": None, "beans_bu": None}
    account = None
    for row in balances.get("data") or []:
        crop = (row.get("crop_name") or "").lower()
        # Only count Ritchie-facility totals (the operator may carry inventory
        # at other Akron Services facilities; we want Ritchie specifically).
        ritchie_total = None
        for lt in row.get("location_totals") or []:
            name = (lt.get("location_name") or "").lower()
            if "ritchie" in name:
                # Prefer numeric form if available; some rows may only carry
                # the formatted string. We re-derive numeric from total when
                # it's a string like "9,115.28 bushels".
                if isinstance(lt.get("total_numeric"), (int, float)):
                    ritchie_total = float(lt["total_numeric"])
                else:
                    s = (lt.get("total") or "").replace(",", "").split()
                    try:
                        ritchie_total = float(s[0]) if s else None
                    except ValueError:
                        ritchie_total = None
        # Fall back to the row total if no per-location breakdown matched
        # (happens when a producer only delivers to one facility).
        if ritchie_total is None and row.get("total_numeric") is not None:
            ritchie_total = float(row["total_numeric"])
        if crop.startswith("corn"):
            out["corn_bu"] = ritchie_total
        elif crop.startswith("soy"):
            out["beans_bu"] = ritchie_total
        if not account:
            account = row.get("account_name")
    return out, account


def fetch_avg_pricing(token: str) -> dict[str, float | None]:
    """Best-effort: hit GetAllContracts and look for Average Pricing Program
    enrollment. Returns {"corn_bu": ..., "soy_bu": ...}, both None if we
    can't find a clean signal — the caller treats this as a nice-to-have.

    Bushel's contract type names vary by tenant; we look for any contract
    whose type name contains 'average' or 'avg' and bucket by commodity.
    """
    out: dict[str, float | None] = {"corn_bu": None, "soy_bu": None}
    try:
        r = requests.post(
            CONTRACTS_URL,
            headers=_api_headers(token),
            json={},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
    except Exception:
        return out

    contracts = body.get("contracts") or body.get("data") or []
    for c in contracts:
        type_name = ((c.get("contractType") or {}).get("name") or c.get("type") or "").lower()
        if "average" not in type_name and "avg" not in type_name:
            continue
        commodity = ((c.get("commodity") or {}).get("name") or "").lower()
        # Quantity field varies — try a few common shapes.
        qty = (c.get("quantity") or c.get("contractQuantity")
               or (c.get("amount") or {}).get("quantity"))
        try:
            qty_f = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty_f = None
        if qty_f is None:
            continue
        if commodity.startswith("corn"):
            out["corn_bu"] = (out["corn_bu"] or 0) + qty_f
        elif commodity.startswith("soy"):
            out["soy_bu"] = (out["soy_bu"] or 0) + qty_f
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--out", type=Path, default=OUT_PATH,
                   help=f"Where to write the sidecar JSON (default {OUT_PATH})")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    fetch_errors: list[str] = []

    user = os.environ.get("BUSHEL_USER") or os.environ.get("AKRON_USER")
    pw   = os.environ.get("BUSHEL_PASS") or os.environ.get("AKRON_PASS")
    if not user or not pw:
        sys.stderr.write("!! BUSHEL_USER / BUSHEL_PASS not set\n")
        return 2

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    # 1. Auth — try ROPC, then portal-mediated.
    if not args.quiet:
        sys.stderr.write("[1/5] auth: trying ROPC then portal-mediated...\n")
    token = get_token_ropc(s, user, pw) or get_token_portal(s, user, pw)
    if not token:
        sys.stderr.write("!! auth failed; refusing to write partial sidecar\n")
        return 2

    # 2. Session metadata (installationId required by centre.bushelops).
    if not args.quiet:
        sys.stderr.write("[2/5] reading session metadata from auth/check...\n")
    try:
        meta = fetch_session_meta(s)
    except Exception as e:
        sys.stderr.write(f"!! could not read session metadata: {e}\n")
        return 2
    if not meta.get("installation_id"):
        sys.stderr.write("!! no installationId in __NEXT_DATA__ — cannot call centre\n")
        return 2

    # 3. Storage balances (the headline number).
    if not args.quiet:
        sys.stderr.write("[3/5] fetching commodity-balances...\n")
    try:
        bal_raw = fetch_commodity_balances(token, meta["installation_id"], meta["version"])
        storage, account = shape_storage(bal_raw)
    except Exception as e:
        sys.stderr.write(f"!! commodity-balances failed: {e}\n")
        return 3
    if storage["corn_bu"] is None and storage["beans_bu"] is None:
        sys.stderr.write("!! no Ritchie balances returned; bailing\n")
        return 3

    # 4. Live bids (also useful for the advisor).
    if not args.quiet:
        sys.stderr.write("[4/5] fetching GetBidsList...\n")
    try:
        bids_raw = fetch_bids_raw(s, token)
        bids = shape_ritchie(bids_raw).get("bids") or {}
    except Exception as e:
        fetch_errors.append(f"bids: {e}")
        bids = {}

    # 5. Average Pricing Program enrollment (best-effort).
    if not args.quiet:
        sys.stderr.write("[5/5] best-effort: AP enrollment from GetAllContracts...\n")
    try:
        ap = fetch_avg_pricing(token)
    except Exception as e:
        fetch_errors.append(f"avg_pricing: {e}")
        ap = {"corn_bu": None, "soy_bu": None}

    payload = {
        "as_of":         _now_iso(),
        "source":        "bushel.commodity-balances + bids",
        "account":       account,
        "location":      RITCHIE_LOCATION_NAME,
        "storage":       storage,
        "avg_pricing":   ap,
        "bids":          bids,
        "fetch_errors":  fetch_errors,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        sys.stderr.write(f"   wrote {args.out}\n")
        sys.stderr.write(
            f"   storage: corn={storage['corn_bu']}  beans={storage['beans_bu']}\n"
            f"   AP:      corn={ap['corn_bu']}        soy={ap['soy_bu']}\n"
            f"   bids:    {sum(len(v) for v in bids.values())} rows\n"
            f"   errors:  {len(fetch_errors)}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
