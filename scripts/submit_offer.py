"""
Submit live orders to Akron/Bushel as standing limit offers.

Reads docs/orders.json for orders with status == "live".
Authenticates via scrape_bushel auth flow, fetches *fresh* ritchieBidLadder,
calls MakeOffer (using current app-version + preferring bid period matching order expiry).
Updates order status → "submitted" (or "submit_error") and writes back.

The promote-order.yml workflow calls this after promote_order.py.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the auth machinery from scrape_bushel.py
sys.path.insert(0, str(Path(__file__).parent))
import scrape_bushel as bushel_auth

HERE   = Path(__file__).parent
DOCS   = HERE.parent / "docs"
ORDERS = DOCS / "orders.json"

MAKE_OFFER_URL = (
    "https://api.bushelpowered.com"
    "/api/markets/aggregator/offers/v1/MakeOffer"
)

# Map order crop names → ritchieBidLadder keys
CROP_KEY = {
    "corn":     "corn",
    "soy":      "soybeans",
    "soybeans": "soybeans",
    "wheat":    "wheat",
}


def pick_bid(ladder: dict, crop: str, target_expiry: str | None = None) -> dict:
    """
    Return a cash bid at Ritchie where canMakeOffer is true.
    Prefers a bid whose period roughly matches the order's expiry month if provided,
    otherwise falls back to the nearest (first) eligible.
    """
    key = CROP_KEY.get(crop.lower())
    if not key:
        raise ValueError(f"Unknown crop {crop!r}")
    eligible = [
        b for b in ladder.get(key, [])
        if b.get("canMakeOffer") and b.get("type") == "cash"
    ]
    if not eligible:
        raise ValueError(
            f"No eligible cash bids for {key} — market may be closed or ladder is stale."
        )

    if target_expiry:
        try:
            # expiry like "2026-08-01" -> look for "Aug" in period
            dt = datetime.strptime(target_expiry, "%Y-%m-%d")
            month = dt.strftime("%b")  # 'Aug'
            for b in eligible:
                if month.lower() in (b.get("period") or "").lower():
                    return b
        except Exception:
            pass  # fall through to nearest

    return eligible[0]  # nearest / first eligible


def make_offer(session, token: str, order: dict, bid: dict, app_version: str = "0.8.84") -> dict:
    headers = {
        "Accept":           "application/json",
        "Authorization":    f"Bearer {token}",
        "Content-Type":     "application/json",
        "Origin":           "https://portal.bushelpowered.com",
        "Referer":          "https://portal.bushelpowered.com/",
        "app-company":      bushel_auth.COMPANY,
        "app-name":         "bushel-web-portal-prod",
        "app-version":      app_version,
    }
    body = {
        "bidId":       bid["id"],
        "quantity":    str(order["bushels"]),
        "offerPrice":  str(order["limit_price"]),
        "expiration":  order.get("expiry"),
    }
    print(
        f"  POST MakeOffer  bidId={bid['id']}  "
        f"qty={order['bushels']}  offerPrice=${order['limit_price']}  "
        f"expiration={order.get('expiry')}"
    )
    r = session.post(MAKE_OFFER_URL, json=body, headers=headers, timeout=30)
    print(f"  HTTP {r.status_code}: {r.text[:600]}")
    r.raise_for_status()
    return r.json()


def extract_offer_id(resp: dict) -> str | None:
    """Pull the offer ID out of whatever shape the response takes."""
    data = resp.get("data") or resp
    if isinstance(data, dict):
        return (
            data.get("id")
            or data.get("offerId")
            or data.get("displayId")
        )
    return None


def main() -> None:
    data = json.loads(ORDERS.read_text(encoding="utf-8"))
    live = [o for o in data.get("orders", []) if o.get("status") == "live"]

    if not live:
        print("No live orders to submit.")
        return

    print(f"Found {len(live)} live order(s).")

    phone    = (os.environ.get("BUSHEL_USER") or os.environ.get("AKRON_USER", "")).strip()
    password = (os.environ.get("BUSHEL_PASS") or os.environ.get("AKRON_PASS", "")).strip()
    if not phone or not password:
        print("!! BUSHEL_USER/BUSHEL_PASS must be set")
        sys.exit(2)

    s       = bushel_auth.make_session()
    sess    = bushel_auth.login(s, phone, password)
    token   = sess["accessToken"]

    # Fresh ladder + version (no more stale docs/bushel.json dependency)
    print("Fetching fresh bids ladder from Bushel API...")
    commodity_bids = bushel_auth.fetch_commodity_bids(s, token)
    ladder = bushel_auth.build_ritchie_bid_ladder(commodity_bids)
    app_version = bushel_auth.fetch_portal_version(s)
    print(f"Using app-version={app_version}")

    # Staleness / market guard
    has_eligible = any(
        any(b.get("canMakeOffer") for b in ladder.get(k, []))
        for k in ladder
    )
    if not has_eligible:
        print("!! WARNING: No bids with canMakeOffer=true in fresh ladder. Offers will likely fail.")

    changed = False
    for order in live:
        oid = order["id"]
        print(f"\n→ {oid}  {order['bushels']} bu {order['crop']} @ ${order['limit_price']}")
        try:
            bid = pick_bid(ladder, order["crop"], order.get("expiry"))
            print(f"  bid: {bid['id']}  {bid.get('period')}  current ${bid.get('bidPrice')}")
            resp     = make_offer(s, token, order, bid, app_version=app_version)
            offer_id = extract_offer_id(resp)
            order["status"]          = "submitted"
            order["bushel_offer_id"] = offer_id
            order["submitted_at"]    = datetime.now(timezone.utc).isoformat()
            print(f"  OK — offer_id={offer_id}")
        except Exception as exc:
            print(f"  ERROR: {exc}")
            order["status"]       = "submit_error"
            order["submit_error"] = str(exc)
            order["errored_at"]   = datetime.now(timezone.utc).isoformat()
        changed = True

    if changed:
        ORDERS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote {ORDERS}")


if __name__ == "__main__":
    main()
