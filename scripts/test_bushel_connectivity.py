#!/usr/bin/env python3
"""
Bushel Connectivity Health Check
Runs the full 8-step Keycloak OIDC auth flow, then hits the bids API.
Sends SMS via TextBelt on any failure.
"""

import os
import sys
import json
import requests
from urllib.parse import urlparse, parse_qs
from html.parser import HTMLParser


BUSHEL_PORTAL = "https://portal.bushelpowered.com"
BUSHEL_API    = "https://api.bushelpowered.com"
COMPANY_SLUG  = "akronservices"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class FormActionParser(HTMLParser):
    """Extract the action URL from the first <form> tag."""
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attrs):
        if tag == "form" and self.action is None:
            attrs_dict = dict(attrs)
            self.action = attrs_dict.get("action")


def parse_form_action(html: str) -> str:
    parser = FormActionParser()
    parser.feed(html)
    if not parser.action:
        raise RuntimeError("Could not find form action in HTML")
    return parser.action


def send_sms(message: str):
    key = os.environ.get("TEXTBELT_KEY", "")
    phones_raw = os.environ.get("ALERT_PHONE", "")
    phones = [p.strip() for p in phones_raw.split(",") if p.strip()]
    for phone in phones:
        try:
            resp = requests.post(
                "https://textbelt.com/text",
                data={"phone": phone, "message": message, "key": key},
                timeout=15,
            )
            result = resp.json()
            if not result.get("success"):
                print(f"WARNING: SMS to {phone} may have failed: {result}", file=sys.stderr)
        except Exception as exc:
            print(f"WARNING: SMS send exception for {phone}: {exc}", file=sys.stderr)


def run_auth_flow() -> str:
    """Execute the full 8-step Keycloak OIDC flow and return the Bearer accessToken."""
    bushel_user = os.environ["BUSHEL_USER"]
    bushel_pass = os.environ["BUSHEL_PASS"]

    session = requests.Session()
    session.headers.update(HEADERS_BASE)

    # Step 1: GET CSRF token
    r1 = session.get(f"{BUSHEL_PORTAL}/api/auth/csrf", timeout=30)
    r1.raise_for_status()
    csrf_token = r1.json()["csrfToken"]

    # Step 2: POST to trigger Keycloak redirect, get auth URL
    r2 = session.post(
        f"{BUSHEL_PORTAL}/api/auth/signin/keycloak?company_slug={COMPANY_SLUG}",
        json={"csrfToken": csrf_token, "callbackUrl": BUSHEL_PORTAL, "json": True},
        timeout=30,
        allow_redirects=False,
    )
    r2.raise_for_status()
    auth_url = r2.json().get("url")
    if not auth_url:
        raise RuntimeError(f"No Keycloak auth URL returned: {r2.text[:200]}")

    # Step 3: GET Keycloak login page, parse form action
    r3 = session.get(auth_url, timeout=30)
    r3.raise_for_status()
    form_action = parse_form_action(r3.text)

    # Step 4: POST username (phone number)
    r4 = session.post(
        form_action,
        data={"username": bushel_user},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=True,
    )
    r4.raise_for_status()
    form_action2 = parse_form_action(r4.text)

    # Step 5: POST password — expect 302 to callback URL
    r5 = session.post(
        form_action2,
        data={"password": bushel_pass},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=False,
    )
    callback_url = r5.headers.get("Location") or r5.headers.get("location")
    if not callback_url:
        raise RuntimeError(
            f"Expected 302 after password POST, got {r5.status_code}; "
            f"body: {r5.text[:200]}"
        )

    # Step 6: GET /api/auth/callback/keycloak to establish session cookies
    r6 = session.get(callback_url, timeout=30, allow_redirects=True)
    r6.raise_for_status()

    # Step 7: GET post-login check URL to obtain per-tenant cookies
    post_login_url = f"{BUSHEL_PORTAL}/api/auth/session"
    r7 = session.get(
        f"{BUSHEL_PORTAL}/?company_slug={COMPANY_SLUG}",
        timeout=30,
        allow_redirects=True,
    )
    # non-fatal if this fails; we still try the session endpoint

    # Step 8: GET /api/auth/session to extract the accessToken
    r8 = session.get(post_login_url, timeout=30)
    r8.raise_for_status()
    session_data = r8.json()
    access_token = (
        session_data.get("accessToken")
        or (session_data.get("user") or {}).get("accessToken")
    )
    if not access_token:
        raise RuntimeError(
            f"No accessToken in session response: {json.dumps(session_data)[:300]}"
        )

    return access_token


def check_bids(token: str):
    """POST to GetBidsList and verify bid data is present."""
    headers = {
        **HEADERS_BASE,
        "Authorization": f"Bearer {token}",
        "app-company": COMPANY_SLUG,
    }
    resp = requests.post(
        f"{BUSHEL_API}/api/markets/aggregator/bids/v1/GetBidsList",
        json={},
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GetBidsList returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    # Accept list or dict with bids key; just confirm non-empty/non-null
    if not data:
        raise RuntimeError("GetBidsList returned empty/null response body")
    return data


def main():
    missing = [v for v in ("BUSHEL_USER", "BUSHEL_PASS", "TEXTBELT_KEY", "ALERT_PHONE") if not os.environ.get(v)]
    if missing:
        reason = f"Missing required env vars: {', '.join(missing)}"
        print(f"ERROR: {reason}", file=sys.stderr)
        send_sms(f"[FARM] Bushel connectivity FAILED: {reason}")
        sys.exit(1)

    try:
        token = run_auth_flow()
    except Exception as exc:
        reason = f"Auth flow error: {exc}"
        print(f"ERROR: {reason}", file=sys.stderr)
        send_sms(f"[FARM] Bushel connectivity FAILED: {reason}")
        sys.exit(1)

    try:
        check_bids(token)
    except Exception as exc:
        reason = f"Bids check error: {exc}"
        print(f"ERROR: {reason}", file=sys.stderr)
        send_sms(f"[FARM] Bushel connectivity FAILED: {reason}")
        sys.exit(1)

    print("OK: Bushel auth + bids live")
    sys.exit(0)


if __name__ == "__main__":
    main()
