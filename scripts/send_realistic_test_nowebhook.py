#!/usr/bin/env python3
"""
ISOLATION TEST: same payload as send_realistic_test.py but with the
TextBelt `replyWebhookUrl` parameter forcibly omitted from every POST.

Purpose: confirm whether TextBelt's "ability to send URLs via text is
limited to verified accounts" rejection is being triggered by the
webhook param itself (a URL TextBelt sees on the wire) rather than by
the SMS body. If THIS script's sends succeed and the original
send_realistic_test.py's still fail, the webhook param is the gate
and the fix is whitelisting the key — not changing the message format.

This script does NOT record pending state (no point in tracking a
pending Y/N when there's no webhook to collect the reply). It's a
pure send-path probe.

Env: TEXTBELT_KEY, ALERT_PHONE — same as the real script.
SIM_* overrides also work the same way.
"""

from __future__ import annotations

import logging
import os
import sys

import httpx


TEXTBELT_KEY = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE  = os.environ.get("ALERT_PHONE",  "")

SIM_COMMODITY = os.environ.get("SIM_COMMODITY", "corn")
SIM_LIVE      = float(os.environ.get("SIM_LIVE",     "4.75"))
SIM_QUANTITY  = int(os.environ.get("SIM_QUANTITY",   "8250"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("send_realistic_test_nowebhook")


def _recipients() -> list[str]:
    return [p.strip() for p in ALERT_PHONE.split(",") if p.strip()]


def _send(phone: str, message: str) -> bool:
    # Note the deliberate omission: NO replyWebhookUrl, even if
    # REPLY_WEBHOOK_URL is set in the environment.
    payload = {"phone": phone, "message": message, "key": TEXTBELT_KEY}
    try:
        r = httpx.post("https://textbelt.com/text", data=payload, timeout=15.0)
        body = r.json()
        log.info("textbelt[%s]: %s", phone, body)
        return bool(body.get("success"))
    except Exception as e:
        log.exception("textbelt call failed for %s: %s", phone, e)
        return False


def _build_message() -> str:
    # Stand-in code so the format mirrors the real alert without
    # needing the signal_key/short_id machinery.
    code = "999999"
    total = SIM_LIVE * SIM_QUANTITY
    risk = int(round((abs(total) * 0.025 * 1.65) / 10) * 10)
    return (
        f"⚠️ {SIM_COMMODITY.upper()}: Sell {SIM_QUANTITY:,} @ ${SIM_LIVE:.2f} "
        f"(${total:,.0f}). 1d risk -${risk:,.0f}. Reply Y {code} / N {code}."
    )


def main() -> int:
    recipients = _recipients()
    if not TEXTBELT_KEY:
        log.error("TEXTBELT_KEY not set; aborting")
        return 1
    if not recipients:
        log.error("ALERT_PHONE not set; aborting")
        return 1

    message = _build_message()
    log.info("message (%d chars): %s", len(message), message)
    log.info("webhook param: OMITTED (isolation test)")

    any_ok = False
    for phone in recipients:
        if _send(phone, message):
            any_ok = True

    if not any_ok:
        log.error("all sends rejected — webhook param is NOT the gate; "
                  "TextBelt is rejecting on the message body itself")
        return 1

    log.info("at least one send accepted — webhook param was the gate; "
             "whitelist the key and the original script will work as-is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
