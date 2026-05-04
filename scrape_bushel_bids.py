"""
Bushel / Akron Services bid scraper.

Authenticates against the Bushel Keycloak realm and fetches the
GetBidsList payload, then filters down to Ritchie Grain Elevator and
emits a clean JSON suitable for the advisor system prompt.

Auth strategy (as of 2026-05; bushel.v2 Keycloak theme):
  1. Try OAuth Resource Owner Password Credentials (ROPC) — one POST,
     done. As of May 2026 Bushel has disabled Direct Access Grants on
     the bushel-web-portal client (returns 401 unauthorized_client),
     so this path is effectively dead but kept for cheap retry if/when
     they re-enable it.
  2. Fall back to the "portal-mediated" flow — let portal.bushelpowered.com
     do the confidential-client token exchange for us, then scrape the
     resulting JWT from the next.js __NEXT_DATA__ blob:
       a. GET /api/auth/csrf                       (next-auth CSRF token)
       b. POST /api/auth/signin/keycloak           (returns Keycloak URL +
                                                    sets next-auth state cookie)
       c. GET that Keycloak URL                    (login page w/ session_code,
                                                    execution, tab_id, client_data
                                                    embedded in inline JS as
                                                    `window.keycloak.urls.loginAction`)
       d. POST username → POST password            (Keycloak v2 identity-first)
       e. Follow the 302 chain into next-auth's    (server-side token exchange
          /api/auth/callback/keycloak              uses the client_secret we
                                                    don't have)
       f. GET /akronservices/auth/check?post_login=1
          and read __NEXT_DATA__.props.session.accessToken

     Why this path: bushel-web-portal is now a confidential client, so the
     direct PKCE-with-no-secret path returns 401 on the /token call. The
     portal's next-auth handler holds the secret, so we let it do the
     exchange and scrape the JWT it ships back to its own client.

Inputs (env or .env):
  BUSHEL_USER  — phone-derived digit string (e.g. 6302122546)
  BUSHEL_PASS

Outputs:
  - stdout: the clean bids JSON (pretty-printed)
  - --out PATH: write the same JSON there
  - exit code 0 = success, 2 = auth fail, 3 = no Ritchie bids found

Designed to run identically on a Mac, in a Cowork sandbox, and on a
GitHub Actions ubuntu-latest runner. Pure stdlib + requests + bs4.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # GHA / sandbox: env comes from the runner

# -----------------------------------------------------------------------
# Constants pulled from the recon HARs (akron_recon/bushel2/).
# Stable values; rotate only if Bushel migrates auth providers again.
# -----------------------------------------------------------------------
KEYCLOAK_BASE  = "https://id.bushelops.com/auth/realms/bushel"
TOKEN_URL      = f"{KEYCLOAK_BASE}/protocol/openid-connect/token"
AUTH_URL       = f"{KEYCLOAK_BASE}/protocol/openid-connect/auth"
KEYCLOAK_CLIENT = "bushel-web-portal"
COMPANY_SLUG   = "akronservices"

PORTAL_BASE    = "https://portal.bushelpowered.com"
PORTAL_CSRF    = f"{PORTAL_BASE}/api/auth/csrf"
PORTAL_SIGNIN  = f"{PORTAL_BASE}/api/auth/signin/keycloak"
PORTAL_AUTH_CHECK = f"{PORTAL_BASE}/{COMPANY_SLUG}/auth/check?post_login=1"
PORTAL_HOME    = f"{PORTAL_BASE}/{COMPANY_SLUG}"

API_BASE       = "https://api.bushelpowered.com"
BIDS_URL       = f"{API_BASE}/api/markets/aggregator/bids/v1/GetBidsList"

RITCHIE_LOCATION_ID   = "8f2a9960-814b-4d65-99a0-59b51191d11d"
RITCHIE_LOCATION_NAME = "Ritchie Grain Elevator"

# Match the captured portal session exactly so this request is
# indistinguishable from a regular Safari-on-iPhone portal refresh
# coming from this account. Don't change without re-capturing a HAR
# from portal.bushelpowered.com on the device the operator uses most.
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
# Headers the portal includes on every api.bushelpowered.com call.
# `app-company` scopes the request to this operator's tenant — Bushel
# uses this to route to the right backend. Without it the API may
# reject or return an empty response.
PORTAL_HEADERS = {
    "Accept":           "*/*",
    "Accept-Language":  "en-US,en;q=0.9",
    "app-company":      COMPANY_SLUG,
    "Origin":           "https://portal.bushelpowered.com",
    "Referer":          "https://portal.bushelpowered.com/",
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-site",
}


# -----------------------------------------------------------------------
# Auth — try ROPC first, fall back to PKCE auth-code if rejected.
# -----------------------------------------------------------------------
def get_token_ropc(session: requests.Session, user: str, pw: str) -> str | None:
    """Returns access_token on success, None if Keycloak rejects ROPC.

    Direct grant — one round trip if enabled. Many Keycloak realms ship
    with this disabled by default; we handle that case in the fallback.
    """
    r = session.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id":  KEYCLOAK_CLIENT,
            "username":   user,
            "password":   pw,
            "scope":      "openid",
        },
        timeout=20,
    )
    if r.status_code == 200:
        return r.json().get("access_token")
    # 400 with error=unauthorized_client → ROPC disabled, expected
    # 401 with error=invalid_grant     → bad password
    try:
        err = r.json()
        sys.stderr.write(
            f"   ROPC: status {r.status_code}, "
            f"error={err.get('error')!r}, desc={err.get('error_description','')!r}\n"
        )
    except Exception:
        sys.stderr.write(f"   ROPC: status {r.status_code}, body: {r.text[:200]!r}\n")
    sys.stderr.write("   falling back to auth-code flow.\n")
    return None


def _pkce_pair() -> tuple[str, str]:
    """Generate (verifier, challenge) for OAuth PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def get_token_portal(session: requests.Session, user: str, pw: str) -> str | None:
    """Portal-mediated login → returns the access JWT.

    Steps (see module docstring for the why):
      1. GET  /api/auth/csrf                       — next-auth CSRF token
      2. POST /api/auth/signin/keycloak            — sets next-auth state cookie,
                                                     returns the Keycloak URL to visit
      3. GET  that Keycloak URL                    — login page; loginAction URL
                                                     is in inline JS
      4. POST username → POST password             — Keycloak v2 identity-first
      5. Follow 302 chain into the portal callback — next-auth does the
                                                     confidential token exchange
                                                     server-side, sets session cookies
      6. GET  /<slug>/auth/check?post_login=1      — extract accessToken from
                                                     __NEXT_DATA__.props.session
    """
    # 1. CSRF
    r = session.get(PORTAL_CSRF, timeout=20)
    if r.status_code != 200:
        sys.stderr.write(f"!! portal step 1 (csrf): status {r.status_code}\n")
        return None
    try:
        csrf = r.json().get("csrfToken")
    except Exception:
        sys.stderr.write(f"!! portal step 1 (csrf): non-JSON body\n")
        return None
    if not csrf:
        sys.stderr.write(f"!! portal step 1 (csrf): no csrfToken in response\n")
        return None

    # 2. signin/keycloak — returns {url: "https://id.bushelops.com/..."}
    r2 = session.post(
        PORTAL_SIGNIN,
        data={
            "csrfToken":   csrf,
            "callbackUrl": PORTAL_HOME,
            "json":        "true",
            "company":     COMPANY_SLUG,
        },
        headers={"Accept": "application/json"},
        timeout=20,
        allow_redirects=False,
    )
    if r2.status_code not in (200, 302):
        sys.stderr.write(f"!! portal step 2 (signin): status {r2.status_code}, body {r2.text[:200]!r}\n")
        return None
    keycloak_url = None
    try:
        keycloak_url = r2.json().get("url")
    except Exception:
        # Some next-auth versions 302 instead of returning JSON
        keycloak_url = r2.headers.get("Location")
    if not keycloak_url:
        sys.stderr.write(f"!! portal step 2 (signin): no Keycloak URL in response\n")
        sys.stderr.write(f"   body[:300]: {r2.text[:300]!r}\n")
        return None

    # 3. GET Keycloak login page
    r3 = session.get(keycloak_url, timeout=20, allow_redirects=True)
    if r3.status_code != 200:
        sys.stderr.write(f"!! portal step 3 (keycloak GET): status {r3.status_code}\n")
        return None
    user_form_action = _parse_form_action(r3.text, "kc-form-login") or _parse_form_action(r3.text)
    if not user_form_action:
        sys.stderr.write(f"!! portal step 3: no loginAction in HTML\n")
        try:
            dbg = Path(__file__).parent / "akron_recon" / "auth_step1_debug.html"
            dbg.parent.mkdir(parents=True, exist_ok=True)
            dbg.write_text(r3.text, encoding="utf-8")
        except Exception:
            pass
        return None

    # 4a. POST username
    r4 = session.post(
        user_form_action,
        data={"username": user},
        timeout=20,
        allow_redirects=False,
    )
    if r4.status_code == 200:
        pass_action = _parse_form_action(r4.text, "kc-form-login") or _parse_form_action(r4.text)
    elif r4.status_code in (302, 303):
        # Some flows redirect to a password page
        r4b = session.get(r4.headers.get("Location", ""), timeout=20)
        pass_action = _parse_form_action(r4b.text, "kc-form-login") or _parse_form_action(r4b.text)
    else:
        sys.stderr.write(f"!! portal step 4a (username POST): status {r4.status_code}\n")
        return None
    if not pass_action:
        sys.stderr.write("!! portal step 4a: no password loginAction after username POST\n")
        return None

    # 4b. POST password — expect 302 to /api/auth/callback/keycloak?code=...
    r5 = session.post(
        pass_action,
        data={"password": pw, "credentialId": ""},
        timeout=20,
        allow_redirects=False,
    )
    if r5.status_code not in (302, 303):
        sys.stderr.write(f"!! portal step 4b (password POST): expected 302, got {r5.status_code}\n")
        msg_match = re.search(r'(class="[^"]*error[^"]*"[^>]*>([^<]+))', r5.text)
        if msg_match:
            sys.stderr.write(f"   error message: {msg_match.group(2).strip()[:200]}\n")
        return None
    callback_url = r5.headers.get("Location", "")
    if "code=" not in callback_url:
        sys.stderr.write(f"!! portal step 4b: no code in callback URL: {callback_url[:200]}\n")
        return None

    # 5. Follow the callback into next-auth (it does the secret-bearing
    #    token exchange and sets the __Secure-next-auth.session-token cookie).
    r6 = session.get(callback_url, timeout=20, allow_redirects=True)
    if r6.status_code >= 400:
        sys.stderr.write(f"!! portal step 5 (next-auth callback): status {r6.status_code}\n")
        return None

    # 6. Hit auth/check and pull accessToken out of __NEXT_DATA__.
    r7 = session.get(PORTAL_AUTH_CHECK, timeout=20, allow_redirects=True)
    if r7.status_code != 200:
        sys.stderr.write(f"!! portal step 6 (auth/check): status {r7.status_code}\n")
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', r7.text, re.S)
    if not m:
        sys.stderr.write("!! portal step 6: no __NEXT_DATA__ in auth/check page\n")
        return None
    try:
        nd = json.loads(m.group(1))
    except Exception as e:
        sys.stderr.write(f"!! portal step 6: __NEXT_DATA__ not valid JSON: {e}\n")
        return None
    token = (((nd.get("props") or {}).get("session") or {}).get("accessToken")
             or (nd.get("props") or {}).get("token"))
    if not token:
        sys.stderr.write("!! portal step 6: no accessToken in __NEXT_DATA__.props.session\n")
        return None
    return token


# Backwards-compat alias — older callers may import the old name.
get_token_authcode = get_token_portal


def _parse_form_action(page_html: str, form_id: str | None = None) -> str | None:
    """Pick the action URL of the login form on a Keycloak page.

    Two shapes to handle:
      1. Classic Keycloak — server-rendered <form id='kc-form-login' action='...'>.
      2. Keycloak v2 (bushel.v2 theme, ~early 2026) — the form is built by JS,
         and the action URL only exists inside an inline script as
         `window.keycloak.urls.loginAction = new URL(htmlDecode('https://...
         /login-actions/authenticate?session_code=...&execution=...&...'))`.
         Same POST contract (form-encoded `username` / `password`) — only
         the URL discovery changes.
    """
    soup = BeautifulSoup(page_html, "lxml")
    if form_id:
        f = soup.find("form", id=form_id)
        if f and f.get("action"):
            return html.unescape(f["action"])
    f = soup.find("form")
    if f and f.get("action"):
        return html.unescape(f["action"])

    # SPA fallback: extract from the inline JS bootstrap.
    m = re.search(
        r"keycloak\.urls\.loginAction\s*=\s*new\s+URL\(\s*htmlDecode\(\s*['\"]([^'\"]+)['\"]",
        page_html,
    )
    if m:
        return html.unescape(m.group(1))
    # Some builds drop the htmlDecode wrapper.
    m = re.search(
        r"keycloak\.urls\.loginAction\s*=\s*new\s+URL\(\s*['\"]([^'\"]+)['\"]",
        page_html,
    )
    if m:
        return html.unescape(m.group(1))
    return None


# -----------------------------------------------------------------------
# Bid fetch + shape
# -----------------------------------------------------------------------
def fetch_bids_raw(session: requests.Session, token: str) -> dict[str, Any]:
    """POST GetBidsList — the response body is base64-encoded JSON.

    The recon HAR shows {"locationSourceIds": null} as the body, which
    asks for "everything visible to this user." We keep that — it
    returns ~16 locations and we filter to Ritchie client-side. If
    the surface area changes, we can pass [RITCHIE_LOCATION_ID] here.
    """
    r = session.post(
        BIDS_URL,
        headers={
            **PORTAL_HEADERS,
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json={"locationSourceIds": None},
        timeout=30,
    )
    r.raise_for_status()
    body = r.text.strip()
    # Bushel returns base64-encoded JSON. Decode if it doesn't already
    # parse as JSON (some endpoints return raw JSON).
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(base64.b64decode(body).decode("utf-8"))


def shape_ritchie(payload: dict[str, Any]) -> dict[str, Any]:
    """Filter to Ritchie + reshape into the compact form the advisor reads.

    Output shape (stable contract for the worker / persona):
      {
        "as_of": "2026-05-01T20:34:00Z",
        "source": "bushel.GetBidsList",
        "location": "Ritchie Grain Elevator",
        "bids": {
          "corn":  [{"month": "May", "cash": 4.48, "basis": -0.17,
                     "futures": 4.6525, "futures_symbol": "ZCK26"}, ...],
          "soy":   [...],
        }
      }
    """
    locs = payload.get("locations") or []
    ritchie = next((l for l in locs if l.get("id") == RITCHIE_LOCATION_ID
                    or l.get("name") == RITCHIE_LOCATION_NAME), None)
    if not ritchie:
        sys.stderr.write(
            f"!! Ritchie location not found in {len(locs)} returned locations\n"
        )
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for grp in ritchie.get("groups", []):
        commodity_name = (grp.get("commodity") or {}).get("name", "").lower()
        # Normalize: 'soybeans' → 'soy' so the advisor sees the same key
        # the rest of the system uses.
        key = "soy" if commodity_name.startswith("soy") else commodity_name
        bids = []
        for b in grp.get("bids", []):
            try:
                bids.append({
                    "month":          b.get("description"),
                    "cash":           _fnum(b.get("bidPrice")),
                    "basis":          _fnum(b.get("basisPrice")),
                    "futures":        _fnum(b.get("futuresPrice")),
                    "futures_symbol": b.get("futuresSymbol"),
                })
            except Exception as e:
                sys.stderr.write(f"   skip bid {b!r}: {e}\n")
        if bids:
            out[key] = bids

    return {
        "as_of":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":   "bushel.GetBidsList",
        "location": RITCHIE_LOCATION_NAME,
        "bids":     out,
    }


def _fnum(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# -----------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", help="Write JSON to this path (in addition to stdout).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress progress messages on stderr.")
    args = p.parse_args()

    user = os.environ.get("BUSHEL_USER") or os.environ.get("AKRON_USER")
    pw   = os.environ.get("BUSHEL_PASS") or os.environ.get("AKRON_PASS")
    if not user or not pw:
        sys.stderr.write(
            "!! BUSHEL_USER / BUSHEL_PASS not set "
            "(falls back to AKRON_USER / AKRON_PASS).\n"
        )
        return 2

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    if not args.quiet:
        sys.stderr.write("[1/3] auth: trying ROPC...\n")
    token = get_token_ropc(s, user, pw)

    if not token:
        if not args.quiet:
            sys.stderr.write("[1/3] auth: trying portal-mediated flow...\n")
        token = get_token_portal(s, user, pw)

    if not token:
        sys.stderr.write("!! auth: both ROPC and portal-mediated paths failed.\n")
        return 2

    if not args.quiet:
        sys.stderr.write("[2/3] fetching GetBidsList...\n")
    raw = fetch_bids_raw(s, token)

    if not args.quiet:
        sys.stderr.write("[3/3] filtering to Ritchie + reshaping...\n")
    shaped = shape_ritchie(raw)
    if not shaped or not shaped.get("bids"):
        sys.stderr.write("!! no Ritchie bids in payload — Bushel may have moved the location.\n")
        return 3

    out_str = json.dumps(shaped, indent=2)
    print(out_str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out_str + "\n", encoding="utf-8")
        if not args.quiet:
            sys.stderr.write(f"   wrote {args.out}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
