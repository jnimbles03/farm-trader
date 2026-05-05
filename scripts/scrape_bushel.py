"""
Bushel (akronservices.com) scraper.

Bushel powers The Akron App / akronservices.com / portal.bushelpowered.com.
Auth is Keycloak OIDC fronted by NextAuth.js on portal.bushelpowered.com.

Why this script exists:
  The website (akronservices.com) and the iOS app are two faces of the
  same Bushel platform. The website's WebForms login wants a phone-derived
  username we don't have; the app accepts phone + password and goes
  through Keycloak. This script replays the iPhone-app auth flow over
  HTTP using requests, then pulls the same data the app does:
    • commodity balances  (bushels on hand by crop / location)
    • cash bids           (current corn/soy bid ladder)
    • contracts           (committed sales — incl. the 1,500-bu Avg Pricing Program)
    • scale tickets       (every load delivered to Ritchie)
    • settlements         (paid sales)
    • invoices            (any payable bills issued by Akron)
    • account-payable balances (current AR/AP)

Quirks this had to solve:
  - Keycloak login is split: POST 1 has the username, POST 2 has the
    password. Each POST returns a new session_code in the next form's
    action URL. Chase the chain by parsing the HTML form action.
  - The API needs Bearer JWT in Authorization, not the NextAuth cookies.
    The JWT lives in NextAuth's session and is exposed by GET /api/auth/session.
  - Every API call also wants `app-company: akronservices` (the tenant slug).
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# python-dotenv is only used to load a local dev .env; in CI the env
# vars come straight from GitHub secrets, so don't hard-fail if it
# isn't installed.
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):  # type: ignore[no-redef]
        return False

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# Accept either env-var pair. The GitHub secret is BUSHEL_USER/BUSHEL_PASS
# (matching refresh_ritchie.py); local .env historically used AKRON_USER/
# AKRON_PASS. Either is fine.
PHONE = (os.environ.get("BUSHEL_USER")
         or os.environ.get("AKRON_USER", "")).strip()
PASS  = (os.environ.get("BUSHEL_PASS")
         or os.environ.get("AKRON_PASS", "")).strip()

# .env's AKRON_USER was Mom's email originally; the actual username is the
# 10-digit phone number. Allow either (strip @gmail.com if someone forgot).
if "@" in PHONE:
    print(f"!! AKRON_USER looks like an email ({PHONE}). The Bushel auth uses Mom's phone number.")
    print(f"   Edit .env to set AKRON_USER=6302122546 (or whatever 10-digit phone Mom registered with).")
    sys.exit(2)

PORTAL  = "https://portal.bushelpowered.com"
ID_HOST = "https://id.bushelops.com"
COMPANY = "akronservices"

OUT = HERE / "akron_recon"
OUT.mkdir(exist_ok=True)

# Headers used for every request — modeled on what Safari/iPhone sent
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def login(s: requests.Session, phone: str, password: str) -> dict:
    """Walk the Keycloak split-flow login. Returns the parsed
       /api/auth/session response (which contains accessToken)."""

    # ── 1. CSRF token from NextAuth ────────────────────────────────
    print("[1] GET /api/auth/csrf")
    r = s.get(f"{PORTAL}/api/auth/csrf", timeout=20,
              headers={"Accept": "application/json"})
    r.raise_for_status()
    csrf = r.json()["csrfToken"]

    # ── 2. Initiate signin → get the Keycloak auth URL ─────────────
    print("[2] POST /api/auth/signin/keycloak (initiate flow)")
    r = s.post(
        f"{PORTAL}/api/auth/signin/keycloak",
        params={"company_slug": COMPANY, "show_cancel_button": "true"},
        data={
            "callbackUrl": f"{PORTAL}/{COMPANY}/auth/check?post_login=1",
            "csrfToken": csrf,
            "json": "true",
        },
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": PORTAL,
            "Referer": f"{PORTAL}/{COMPANY}/welcome",
        },
        timeout=20,
    )
    r.raise_for_status()
    keycloak_auth_url = r.json()["url"]
    print(f"    → keycloak auth URL acquired ({len(keycloak_auth_url)} chars)")

    # ── 3. GET the Keycloak login page; pull the form action ───────
    print("[3] GET keycloak login page")
    r = s.get(keycloak_auth_url, timeout=20,
              headers={"Referer": f"{PORTAL}/", "Accept": "text/html"})
    r.raise_for_status()
    form_action = _find_form_action(r.text, "login-actions/authenticate")
    if not form_action:
        raise RuntimeError("Could not find Keycloak login form action on auth page")
    form_action = urljoin(ID_HOST, form_action)

    # ── 4. POST 1: username ────────────────────────────────────────
    print(f"[4] POST 1 (username) to keycloak")
    r = s.post(
        form_action,
        data={"username": phone},
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "null",  # Safari sends literal 'null' here
            "Referer": keycloak_auth_url,
        },
        timeout=20,
        allow_redirects=False,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Username POST returned {r.status_code} (expected 200 with new form)")
    form_action_2 = _find_form_action(r.text, "login-actions/authenticate")
    if not form_action_2:
        # Save the response so we can inspect what went wrong
        (OUT / "bushel_username_error.html").write_text(r.text, encoding="utf-8")
        raise RuntimeError("No password form returned — username may be wrong. "
                           "See akron_recon/bushel_username_error.html")
    form_action_2 = urljoin(ID_HOST, form_action_2)

    # ── 5. POST 2: password ────────────────────────────────────────
    print(f"[5] POST 2 (password) to keycloak")
    r = s.post(
        form_action_2,
        data={"password": password},
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "null",
            "Referer": form_action,
        },
        timeout=20,
        allow_redirects=False,
    )
    if r.status_code != 302:
        # Save snapshot — most likely "Invalid credentials" page
        (OUT / "bushel_password_error.html").write_text(r.text, encoding="utf-8")
        raise RuntimeError(f"Password POST returned {r.status_code} (expected 302). "
                           f"See akron_recon/bushel_password_error.html")
    callback_url = r.headers["Location"]
    print(f"    → 302 to {callback_url[:80]}…")

    # ── 6. Hit the NextAuth callback (sets session-token cookies) ──
    print("[6] GET /api/auth/callback/keycloak")
    r = s.get(callback_url, timeout=20,
              headers={"Accept": "text/html", "Referer": form_action_2},
              allow_redirects=False)
    if r.status_code != 302:
        raise RuntimeError(f"Callback returned {r.status_code}, expected 302")
    final_url = urljoin(PORTAL, r.headers["Location"])

    # ── 7. Visit the post-login check (gets per-tenant cookies) ────
    print(f"[7] GET {final_url}")
    s.get(final_url, timeout=20,
          headers={"Accept": "text/html", "Referer": PORTAL},
          allow_redirects=True)

    # ── 8. /api/auth/session → access token ────────────────────────
    print("[8] GET /api/auth/session (extract Bearer token)")
    r = s.get(f"{PORTAL}/api/auth/session", timeout=20,
              headers={"Accept": "application/json"})
    r.raise_for_status()
    session = r.json()
    if not session.get("accessToken"):
        raise RuntimeError(f"Session response has no accessToken: {json.dumps(session)[:200]}")
    print(f"    → got accessToken ({len(session['accessToken'])} chars), "
          f"user={session.get('user', {}).get('name', '?')}")
    return session


def _find_form_action(html: str, must_contain: str) -> str | None:
    """First try a real <form> tag (in case Keycloak ever serves the
       traditional template). If that fails, fall back to scraping
       the auth params out of the JS-rendered page — Keycloak's modern
       theme injects session_code / execution / tab_id / client_data
       as bare strings inside the page so the JS can build the POST
       URL itself."""
    soup = BeautifulSoup(html, "lxml")
    for f in soup.find_all("form"):
        action = f.get("action") or ""
        if must_contain in action:
            return action

    # JS-rendered fallback. Pull each param off the raw HTML.
    if must_contain == "login-actions/authenticate":
        params = {}
        for k in ("session_code", "execution", "tab_id", "client_data"):
            # Match key=VAL where VAL is everything up to the next quote,
            # ampersand, whitespace, or closing brace.
            m = re.search(rf'{k}=([^"\'&\s)]+)', html)
            if m:
                params[k] = m.group(1)
        if "session_code" in params and "execution" in params:
            from urllib.parse import urlencode
            qs = urlencode({
                "session_code": params["session_code"],
                "execution":    params["execution"],
                "client_id":    "bushel-web-portal",
                "tab_id":       params.get("tab_id", ""),
                "client_data":  params.get("client_data", ""),
            })
            return f"/auth/realms/bushel/login-actions/authenticate?{qs}"
    return None


def fetch_data(s: requests.Session, access_token: str) -> dict:
    """Fetch every data endpoint that the cash flow pro forma cares
       about. Returns a dict keyed by friendly name."""

    # Common headers for every API call
    api_headers_post = {
        "Accept": "*/*",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Origin": PORTAL,
        "Referer": f"{PORTAL}/",
        "app-company": COMPANY,
    }
    api_headers_get = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": PORTAL,
        "Referer": f"{PORTAL}/",
        "app-company": COMPANY,
        "app-name": "bushel-web-portal-prod",
        "app-version": "0.8.84",
    }

    out = {}

    # api.bushelpowered.com endpoints (POST with JSON body)
    api_calls = [
        ("commodityBids",
         "POST", "https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList",
         {"locationSourceIds": None}),
        ("contractsSummary",
         "POST", "https://api.bushelpowered.com/api/aggregator/grain/v1/GetContractsSummaries",
         {}),
        ("contracts",
         "POST", "https://api.bushelpowered.com/api/aggregator/grain/v1/GetAllContracts",
         {}),
        ("offers",
         "POST", "https://api.bushelpowered.com/api/markets/aggregator/offers/v1/ListOffers",
         {}),
        ("accounts",
         "POST", "https://api.bushelpowered.com/api/aggregator/accounts/v1/GetAllAccounts",
         {}),
        ("invoices",
         "POST", "https://api.bushelpowered.com/api/aggregator/invoices/v1/ListInvoices",
         {}),
        ("accountPayableBalances",
         "POST", "https://api.bushelpowered.com/api/aggregator/accountpayablebalance/v1/GetAccountPayableBalances",
         {}),
    ]

    for key, method, url, body in api_calls:
        print(f"  fetch {key}…", end=" ", flush=True)
        try:
            r = s.request(method, url, json=body, headers=api_headers_post, timeout=30)
            r.raise_for_status()
            out[key] = r.json()
            print(f"ok ({len(r.content)} bytes)")
        except Exception as e:
            print(f"ERR {e}")
            out[key] = {"error": str(e)}

    # centre.bushelops.com endpoints
    centre_calls = [
        ("commodityBalances",
         "GET", "https://centre.bushelops.com/api/v2/commodity-balances", None),
        ("scaleTickets",
         "POST", "https://centre.bushelops.com/api/v3/tickets?page=1&simple-paging=1", {}),
        ("scaleTicketsSummary",
         "POST", "https://centre.bushelops.com/api/v1/summaries/tickets", {}),
        ("settlements",
         "POST", "https://centre.bushelops.com/api/v2/settlements?page=1", {}),
    ]

    for key, method, url, body in centre_calls:
        print(f"  fetch {key}…", end=" ", flush=True)
        try:
            if method == "GET":
                r = s.get(url, headers=api_headers_get, timeout=30)
            else:
                r = s.post(url, json=body or {}, headers={**api_headers_get, "Content-Type": "application/json"},
                           timeout=30)
            r.raise_for_status()
            out[key] = r.json()
            print(f"ok ({len(r.content)} bytes)")
        except Exception as e:
            print(f"ERR {e}")
            out[key] = {"error": str(e)}

    return out


def summarize_for_cashflow(data: dict) -> dict:
    """Distill the raw API output into the values the Cash Flow Pro
       Forma directly needs. Keep this tight — the dashboard reads
       these top-level keys; everything else stays in `raw` for later."""
    out = {
        "fetchedAt": None,  # filled in by caller
        "company": COMPANY,
        "account": None,
        "bushelsOnHand": {},  # {commodity: {bushels, location}}
        "bids": {},           # {commodity: {nearest_period, price}}
        "contractsOpen": [],  # list of unpriced/unfulfilled contracts
        "balanceOwed": 0.0,   # sum of payable balances
    }

    # Account name. Prefer the GetAllAccounts endpoint, fall back to
    # the account_name embedded in commodity-balances (which works even
    # when GetAllAccounts 400s).
    accs = (data.get("accounts") or {}).get("data") or []
    if accs:
        out["account"] = accs[0].get("displayName")
    else:
        for row in (data.get("commodityBalances") or {}).get("data", []):
            if row.get("account_name"):
                out["account"] = row["account_name"]; break

    # Bushels on hand by crop
    for row in (data.get("commodityBalances") or {}).get("data", []):
        crop = row.get("crop_name", "").lower()
        if crop:
            out["bushelsOnHand"][crop] = {
                "bushels": row.get("total_numeric"),
                "displayBushels": row.get("total"),
                "location": (row.get("location_totals") or [{}])[0].get("location_name"),
            }

    # Nearest-period cash bids by crop. Pin to Ritchie's elevator —
    # that's where the grain physically is, so it's the price we'd
    # actually realize. Other locations are interesting context but
    # not actionable without trucking. Also keep a `bidsByLocation`
    # block so the dashboard can show "Ritchie vs. best in area".
    out["bidsByLocation"] = {}
    PREFERRED_LOC = "Ritchie Grain Elevator"
    bid_alts = {}  # commodity → list of (location, price)
    for loc in (data.get("commodityBids") or {}).get("locations", []):
        loc_name = loc.get("name", "")
        for grp in loc.get("groups", []):
            commodity = (grp.get("commodity") or {}).get("name", "").lower()
            bids = grp.get("bids") or []
            if not commodity or not bids:
                continue
            cash_bids = [b for b in bids if b.get("bidType") == "cash"]
            if not cash_bids:
                continue
            first = cash_bids[0]
            entry = {
                "price": float(first.get("bidPrice", 0)),
                "period": first.get("description"),
                "futuresSymbol": first.get("futuresSymbol"),
                "futuresPrice": float(first.get("futuresPrice")) if first.get("futuresPrice") else None,
                "basis": float(first.get("basisPrice")) if first.get("basisPrice") else None,
                "location": loc_name,
            }
            bid_alts.setdefault(commodity, []).append(entry)
            if loc_name == PREFERRED_LOC:
                out["bids"][commodity] = entry

    # If Ritchie is missing for any reason, fall back to the highest
    # nearby bid (so the dashboard never has empty fields).
    for commodity, alts in bid_alts.items():
        if commodity not in out["bids"]:
            best = max(alts, key=lambda x: x["price"])
            best["fallback"] = True
            out["bids"][commodity] = best
        # Also expose top-3 alternates per commodity so the dashboard
        # can show "best price in area" and the spread vs. Ritchie.
        out["bidsByLocation"][commodity] = sorted(alts, key=lambda x: -x["price"])[:5]

    # Open contracts (any quantity remaining). Field names confirmed
    # from the live API: commodityName, quantityRemaining (string),
    # pricingStatus, deliverySchedule (list of {locationName,
    # deliveryPeriods[]}), contractType, displayType.
    for c in (data.get("contracts") or {}).get("data", []):
        try:
            remaining = float(c.get("quantityRemaining") or 0)
        except (TypeError, ValueError):
            remaining = 0
        if remaining <= 0:
            continue
        # Flatten the delivery schedule to a single readable string
        sched = c.get("deliverySchedule") or []
        delivery = ""
        if sched:
            ds0 = sched[0]
            periods = " · ".join(ds0.get("deliveryPeriods") or [])
            loc = ds0.get("locationName", "")
            delivery = f"{loc} · {periods}" if periods else loc
        out["contractsOpen"].append({
            "displayId": c.get("displayId"),
            "commodity": c.get("commodityName"),
            "type": c.get("displayType"),
            "remainingBushels": remaining,
            "displayRemaining": c.get("displayQuantityRemaining"),
            "delivery": delivery,
            "pricingStatus": c.get("pricingStatus"),
        })

    # Total payable balance + per-account breakdown
    total = 0.0
    out["accountPayableBalances"] = []
    for b in (data.get("accountPayableBalances") or {}).get("data", []):
        bal = float(b.get("accountBalanceNumeric") or 0)
        total += bal
        out["accountPayableBalances"].append({
            "displayId": b.get("displayId"),
            "accountName": b.get("accountName"),
            "balance": bal,
            "displayBalance": b.get("accountBalance"),
        })
    out["balanceOwed"] = total

    # Full Ritchie bid ladder (all months/types, not just nearest cash)
    out["ritchieBidLadder"] = {}
    for loc in (data.get("commodityBids") or {}).get("locations", []):
        if loc.get("name") != "Ritchie Grain Elevator":
            continue
        for grp in loc.get("groups", []):
            commodity = (grp.get("commodity") or {}).get("name", "").lower()
            if not commodity:
                continue
            ladder = []
            for b in grp.get("bids", []):
                ladder.append({
                    "id": b.get("id"),
                    "type": b.get("bidType"),
                    "period": b.get("description"),
                    "bidPrice": float(b.get("bidPrice")) if b.get("bidPrice") else None,
                    "basisPrice": float(b.get("basisPrice")) if b.get("basisPrice") else None,
                    "futuresPrice": float(b.get("futuresPrice")) if b.get("futuresPrice") else None,
                    "futuresSymbol": b.get("futuresSymbol"),
                    "canMakeOffer": (b.get("operations") or {}).get("makeOffer", False),
                })
            out["ritchieBidLadder"][commodity] = ladder

    # All contracts (open AND closed) — useful for historical context
    out["allContracts"] = []
    for c in (data.get("contracts") or {}).get("data", []):
        try:
            remaining = float(c.get("quantityRemaining") or 0)
        except (TypeError, ValueError):
            remaining = 0
        sched = c.get("deliverySchedule") or []
        delivery = ""
        if sched:
            ds0 = sched[0]
            periods = " · ".join(ds0.get("deliveryPeriods") or [])
            loc = ds0.get("locationName", "")
            delivery = f"{loc} · {periods}" if periods else loc
        out["allContracts"].append({
            "displayId": c.get("displayId"),
            "commodity": c.get("commodityName"),
            "type": c.get("displayType"),
            "isClosed": c.get("isClosed", False),
            "remainingBushels": remaining,
            "displayContracted": c.get("displayQuantityContracted"),
            "displayDelivered": c.get("displayQuantityDelivered"),
            "displayRemaining": c.get("displayQuantityRemaining"),
            "delivery": delivery,
            "pricingStatus": c.get("pricingStatus"),
        })

    # Open offers (any standing limit orders Mom has placed at the elevator)
    out["openOffers"] = []
    for o in (data.get("offers") or {}).get("data", []):
        out["openOffers"].append({
            "displayId": o.get("displayId"),
            "commodity": (o.get("commodity") or {}).get("name") if isinstance(o.get("commodity"), dict) else o.get("commodity"),
            "quantity": o.get("displayQuantity") or o.get("quantity"),
            "targetPrice": o.get("displayTargetPrice") or o.get("targetPrice"),
            "status": o.get("status") or o.get("displayStatus"),
            "expirationDate": o.get("expirationDate") or o.get("displayExpirationDate"),
            "raw": o,  # keep full record so the table can show all fields
        })

    return out


def main():
    if not PHONE or not PASS:
        print("!! BUSHEL_USER/BUSHEL_PASS (or AKRON_USER/AKRON_PASS) must be set"); sys.exit(2)

    s = make_session()
    session = login(s, PHONE, PASS)
    access_token = session["accessToken"]

    print()
    print("=== fetching data endpoints ===")
    raw = fetch_data(s, access_token)

    summary = summarize_for_cashflow(raw)

    # Write everything: raw responses for debugging / future expansion,
    # plus the slim summary the dashboard uses.
    from datetime import datetime
    summary["fetchedAt"] = datetime.utcnow().isoformat() + "Z"

    raw_path = HERE.parent / "docs" / "bushel_raw.json"
    feed_path = HERE.parent / "docs" / "bushel.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    feed_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print(f"wrote raw    → {raw_path}")
    print(f"wrote feed   → {feed_path}")
    print()
    print("=== summary the dashboard will read ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
