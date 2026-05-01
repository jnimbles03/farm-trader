#!/usr/bin/env python3
"""
One-shot [FARM TEST] broadcast to a fixed list of family numbers.

Fires the canonical group-confirmation test SMS to 6 hardcoded recipients
(3 required, 3 optional), records the send as pending in
state/confirmations.json, and wires replyWebhookUrl so YES/Y replies are
captured by the Cloudflare worker -> collect-reply.yml pipeline.

This is a self-contained variant of send_realistic_test.py. The message
text is fixed (not a simulated grain alert) and the recipient list is
hardcoded, so the only secrets the workflow needs are TEXTBELT_KEY and
REPLY_WEBHOOK_URL.

Env (required):

  TEXTBELT_KEY       paid TextBelt key
  REPLY_WEBHOOK_URL  Cloudflare Worker URL for Y/N replies

Required group (must reply YES):
  - Maryann Meyer    +16302122546
  - Dan Cooke        +13126361666
  - Sue Lindeen      +17087109387

Optional group (reply appreciated):
  - Patrick Meyer    +16302479950
  - Kevin Cooke      +17084203265
  - Alison Lindeen   +17087109385
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT               = Path(__file__).resolve().parent.parent
STATE_DIR          = ROOT / "state"
DOCS_DIR           = ROOT / "docs"
CONFIRMATIONS_FILE = STATE_DIR / "confirmations.json"
PUBLIC_CONF_FILE   = DOCS_DIR / "confirmations.json"

TEXTBELT_KEY      = os.environ.get("TEXTBELT_KEY", "")
REPLY_WEBHOOK_URL = os.environ.get("REPLY_WEBHOOK_URL", "")

# Soft-test modes (set by workflow inputs).
# DRY_RUN: log what would send, skip TextBelt POST and state write entirely.
# SOLO:    restrict recipients to Patrick's number only.
def _truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}

DRY_RUN = _truthy(os.environ.get("DRY_RUN", ""))
SOLO    = _truthy(os.environ.get("SOLO", ""))
SOLO_PHONE = "+16302479950"  # Patrick

REQUIRED = [
    ("Maryann Meyer", "+16302122546"),
    ("Dan Cooke",     "+13126361666"),
    ("Sue Lindeen",   "+17087109387"),
]
OPTIONAL = [
    ("Patrick Meyer",  "+16302479950"),
    ("Kevin Cooke",    "+17084203265"),
    ("Alison Lindeen", "+17087109385"),
]

# Sample soybean trade confirmation — sized to match the 2026 Average
# Pricing Program tranche (1,000 bu). 1-day risk uses the same
# 2.5% * 1.65σ band as send_realistic_test.py: $10,450 * 0.025 * 1.65
# ≈ $430.
MESSAGE = (
    "[FARM TEST] Group confirmation request — reply YES or Y to confirm. "
    "I'll let everyone know as confirmations come in.\n\n"
    "Sample trade: SELL 1,000 bu Nov '26 soybeans @ $10.45 ($10,450). "
    "Daily @ Risk ±$430.\n\n"
    "Required: Maryann, Dan, Susan.\n"
    "Optional: Patrick, Kevin, Alison.\n\n"
    "One-time test, thanks."
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("send_farm_test_broadcast")


# ---------------------------------------------------------------------------
# State I/O (mirrors collect_reply.py / remind_pending.py)
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not CONFIRMATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMATIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("confirmations.json corrupt, refusing to overwrite: %s", e)
        sys.exit(1)


def _save(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    CONFIRMATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    sanitized: dict[str, dict] = {}
    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        recipients = entry.get("recipients", {})
        sanitized[sid] = {
            "signal_key": entry.get("signal_key"),
            "sent_at":    entry.get("sent_at"),
            "status":     entry.get("status"),
            "total":      len(recipients),
            "yes":        sum(1 for r in recipients.values() if r.get("vote") == "Y"),
            "no":         sum(1 for r in recipients.values() if r.get("vote") == "N"),
        }
    DOCS_DIR.mkdir(exist_ok=True)
    PUBLIC_CONF_FILE.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True) + "\n"
    )


def _short_id(signal_key: str, when: datetime) -> str:
    payload = f"{signal_key}|{when.isoformat()}".encode()
    return hashlib.sha256(payload).hexdigest()[:6]


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def _send(phone: str, message: str) -> bool:
    payload = {"phone": phone, "message": message, "key": TEXTBELT_KEY}
    if REPLY_WEBHOOK_URL:
        payload["replyWebhookUrl"] = REPLY_WEBHOOK_URL
    try:
        r = httpx.post("https://textbelt.com/text", data=payload, timeout=15.0)
        body = r.json()
        log.info("textbelt[%s]: %s", phone, body)
        if body.get("success"):
            return True
        # Fallback: TextBelt rejects replyWebhookUrl on un-whitelisted accounts.
        if "replyWebhookUrl" in payload and "url" in (body.get("error") or "").lower():
            log.warning(
                "textbelt[%s]: replyWebhookUrl rejected — retrying without webhook. "
                "Inbound replies will NOT auto-record. "
                "Whitelist at https://textbelt.com/whitelist to enable.",
                phone,
            )
            payload.pop("replyWebhookUrl")
            r2 = httpx.post("https://textbelt.com/text", data=payload, timeout=15.0)
            body2 = r2.json()
            log.info("textbelt[%s] (no webhook): %s", phone, body2)
            return bool(body2.get("success"))
        return False
    except Exception as e:
        log.exception("textbelt call failed for %s: %s", phone, e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # In dry-run we skip TextBelt entirely, so the key isn't required.
    if not DRY_RUN and not TEXTBELT_KEY:
        log.error("TEXTBELT_KEY not set; aborting")
        return 1
    if not DRY_RUN and not REPLY_WEBHOOK_URL:
        log.warning(
            "REPLY_WEBHOOK_URL not set — replies will not be auto-recorded. "
            "Continuing anyway."
        )

    if SOLO:
        recipients = [("Patrick Meyer (solo)", SOLO_PHONE)]
        log.info("SOLO mode: sending only to Patrick (%s)", SOLO_PHONE)
    else:
        recipients = REQUIRED + OPTIONAL

    if DRY_RUN:
        log.info("=" * 60)
        log.info("DRY RUN — no SMS will be sent, no state will be written")
        log.info("=" * 60)

    log.info("message (%d chars):\n%s", len(MESSAGE), MESSAGE)
    log.info(
        "recipients: %d required + %d optional = %d total%s",
        len(REQUIRED) if not SOLO else 0,
        len(OPTIONAL) if not SOLO else 0,
        len(recipients),
        " (SOLO)" if SOLO else "",
    )
    for name, phone in recipients:
        log.info("  -> %-22s %s", name, phone)

    if DRY_RUN:
        log.info("dry-run complete; exiting 0 without sending or writing state")
        return 0

    now = datetime.now(timezone.utc)
    signal_key = f"farm-test|group-confirmation|{now.strftime('%Y-%m-%dT%H:%M:%S')}"
    if SOLO:
        signal_key = "solo|" + signal_key
    sid = _short_id(signal_key, now)

    any_ok = False
    sent_status: dict[str, bool] = {}
    for name, phone in recipients:
        ok = _send(phone, MESSAGE)
        sent_status[phone] = ok
        if ok:
            any_ok = True
            log.info("  [OK]   %-22s %s", name, phone)
        else:
            log.warning("  [FAIL] %-22s %s", name, phone)

    if not any_ok:
        log.error("no recipient accepted; not recording pending state")
        return 1

    data = _load()
    data[sid] = {
        "signal_key": signal_key,
        "sent_at":    now.isoformat(),
        "message":    MESSAGE,
        "status":     "pending",
        "kind":       "farm-test-broadcast" + ("-solo" if SOLO else ""),
        "required":   [phone for _, phone in (REQUIRED if not SOLO else [])],
        "optional":   [phone for _, phone in (OPTIONAL if not SOLO else recipients)],
        "recipients": {
            phone: {"vote": None, "name": name, "send_ok": sent_status.get(phone, False)}
            for name, phone in recipients
        },
    }
    _save(data)
    log.info("recorded pending sid=%s; YES/Y replies will be collected", sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
