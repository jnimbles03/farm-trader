"""
Submit live orders to Akron/Bushel as standing limit offers.

Reads docs/orders.json for orders with status == "live".
Reads docs/bushel.json for ritchieBidLadder bid IDs.
Authenticates via scrape_bushel auth flow, calls MakeOffer.
Updates order status → "submitted" (or "submit_error") and writes back.

The promote-order.yml workflow calls this after promote_order.py.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the auth machinery from scrape_bushel.py
sys.path.insert(0, str(Path(__file__).parent))
import scrape_bushel as bushel_auth

HERE   = Path(__file__).parent
DOCS   = HERE.parent / "docs"
ORDERS = DOCS / "orders.json"
BUSHEL = DOCS / "bushel.json"

# Candidate endpoints — the original /MakeOffer was returning 404 "default backend".
# We try several common variations until one succeeds.
MAKE_OFFER_CANDIDATES = [
    "https://api.bushelpowered.com/api/markets/aggregator/offers/v1/CreateOffer",  # this one at least parses the request type
    "https://api.bushelpowered.com/api/markets/aggregator/offers/v1/MakeOffer",
    "https://api.bushelpowered.com/api/markets/aggregator/offers/MakeOffer",
    "https://api.bushelpowered.com/api/aggregator/offers/v1/MakeOffer",
    "https://api.bushelpowered.com/api/markets/aggregator/offers/v1/offer",
    "https://api.bushelpowered.com/api/markets/aggregator/offers",
]

# Map order crop names → ritchieBidLadder keys in bushel.json
CROP_KEY = {
    "corn":     "corn",
    "soy":      "soybeans",
    "soybeans": "soybeans",
    "wheat":    "wheat",
}


def pick_bid(ladder: dict, crop: str) -> dict:
    """
    Return the nearest cash bid at Ritchie where canMakeOffer is true.
    The ladder is already sorted by delivery period (nearest first).
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
            f"No eligible cash bids for {key} — market may be closed or "
            "bushel.json is stale."
        )
    return eligible[0]


def make_offer(session, token: str, order: dict, bid: dict) -> dict:
    # Try to pull installation_id for extra header (some endpoints need it)
    installation_id = None
    try:
        check_url = f"{bushel_auth.PORTAL}/{bushel_auth.COMPANY}/auth/check?post_login=1"
        rcheck = session.get(check_url, timeout=20, headers={"Accept": "text/html"})
        if rcheck.ok:
            m = re.search(r'__NEXT_DATA__[^>]*>(.+?)</script>', rcheck.text, re.S)
            if m:
                nd = json.loads(m.group(1))
                installation_id = (nd.get("props") or {}).get("installationId")
    except Exception:
        pass

    base_headers = {
        "Accept":           "application/json",
        "Authorization":    f"Bearer {token}",
        "Content-Type":     "application/json",
        "Origin":           "https://portal.bushelpowered.com",
        "Referer":          "https://portal.bushelpowered.com/",
        "app-company":      bushel_auth.COMPANY,
        "app-name":         "bushel-web-portal-prod",
        "app-version":      "0.8.84",
    }
    if installation_id:
        base_headers["app-installation-id"] = installation_id

    # Build CreateOfferRequest body based on server errors + observed offer shape
    # Required per error: price, accountId, expiration, comments
    # Dynamically fetch the account id using the same endpoint the recon uses.
    account_id = None
    try:
        # Use headers closer to what works for GetAllAccounts in the recon script
        acc_headers = {
            "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://portal.bushelpowered.com",
            "Referer": "https://portal.bushelpowered.com/",
            "app-company": bushel_auth.COMPANY,
            "app-name": "bushel-web-portal-prod",
            "app-version": "0.8.84",
        }
        if installation_id:
            acc_headers["app-installation-id"] = installation_id

        acc_r = session.post(
            "https://api.bushelpowered.com/api/aggregator/accounts/v1/GetAllAccounts",
            json={},
            headers=acc_headers,
            timeout=30,
        )
        print(f"  GetAllAccounts HTTP {acc_r.status_code}")
        if acc_r.ok:
            acc_data = acc_r.json()
            accs = (acc_data.get("data") or [])
            if accs:
                acc0 = accs[0]
                account_id = acc0.get("id") or acc0.get("accountId") or acc0.get("account_id")
                print(f"  using dynamic account_id from GetAllAccounts: {account_id}")
                print(f"    account keys: {list(acc0.keys())[:6]}")
        else:
            print(f"    GetAllAccounts body: {acc_r.text[:300]}")
    except Exception as e:
        print(f"  WARN fetching accounts: {e}")

    if not account_id:
        account_id = "0ad00800-37b3-4294-a512-a26c767a441f"  # last-resort fallback
        print(f"  using fallback account_id: {account_id}")

    body = {
        "bidId":       bid["id"],
        "price":       str(order["limit_price"]),
        "quantity":    str(order["bushels"]),
        "accountId":   account_id,
        "expiration":  order.get("expiry"),
        "comments":    "",
        "offerType":   "cash",
        "unitOfMeasure": "Bushels",
        "locationName": "Ritchie Grain Elevator",
    }

    print(f"  bid: {bid['id']}  {bid.get('period')}  current ${bid.get('bidPrice')}")
    print(f"  trying {len(MAKE_OFFER_CANDIDATES)} candidate MakeOffer endpoints...")

    last_err = None
    for url in MAKE_OFFER_CANDIDATES:
        print(f"  POST {url}  bidId={bid['id']} qty={order['bushels']} target=${order['limit_price']}")
        try:
            r = session.post(url, json=body, headers=base_headers, timeout=30)
            print(f"    HTTP {r.status_code}: {r.text[:400]}")
            if r.status_code < 400:
                return r.json()
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            print(f"    ERR {e}")
            last_err = str(e)

    # If none worked, raise with last error
    raise Exception(f"All MakeOffer candidates failed. Last: {last_err}")


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

    ladder = json.loads(BUSHEL.read_text(encoding="utf-8")).get("ritchieBidLadder", {})

    phone    = (os.environ.get("BUSHEL_USER") or os.environ.get("AKRON_USER", "")).strip()
    password = (os.environ.get("BUSHEL_PASS") or os.environ.get("AKRON_PASS", "")).strip()
    if not phone or not password:
        print("!! BUSHEL_USER/BUSHEL_PASS must be set")
        sys.exit(2)

    s       = bushel_auth.make_session()
    session = bushel_auth.login(s, phone, password)
    token   = session["accessToken"]

    changed = False
    for order in live:
        oid = order["id"]
        print(f"\n→ {oid}  {order['bushels']} bu {order['crop']} @ ${order['limit_price']}")
        try:
            bid = pick_bid(ladder, order["crop"])
            print(f"  bid: {bid['id']}  {bid.get('period')}  current ${bid.get('bidPrice')}")
            resp     = make_offer(s, token, order, bid)
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
