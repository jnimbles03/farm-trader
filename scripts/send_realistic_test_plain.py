#!/usr/bin/env python3
"""
URL-safe realistic SMS smoke test.

Sends a production-shape alert SMS (verb + contract + prices + bushels +
"Reply Y/N") and records it as pending in state/confirmations.json so
remind-pending.yml fires the 5-min reminder on schedule.

Unlike send_realistic_test.py this script never sets replyWebhookUrl or
any other URL-bearing payload field. That keeps the send compatible with
Textbelt accounts that have not been whitelisted for URL features, while
still exercising the realistic message template and pending-state path.

Env (set as workflow secrets or exported locally):

  TEXTBELT_KEY   paid TextBelt key
  ALERT_PHONE    comma-separated E.164 numbers

Optional overrides:

  SIM_COMMODITY   default "corn"
  SIM_CONTRACT    default "Dec '26"
  SIM_LIVE        default "4.75"   (USD/bu)
  SIM_TARGET      default "4.74"   (USD/bu)
  SIM_QUANTITY    default "8250"   (bushels)
  SIM_TRANCHE     default "Tranche 1 (25%)"
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

TEXTBELT_KEY  = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE   = os.environ.get("ALERT_PHONE", "")

SIM_COMMODITY = os.environ.get("SIM_COMMODITY", "corn")
SIM_CONTRACT  = os.environ.get("SIM_CONTRACT",  "Dec '26")
SIM_LIVE      = float(os.environ.get("SIM_LIVE",     "4.75"))
SIM_TARGET    = float(os.environ.get("SIM_TARGET",   "4.74"))
SIM_QUANTITY  = int(os.environ.get("SIM_QUANTITY",   "8250"))
SIM_TRANCHE   = os.environ.get("SIM_TRANCHE",    "Tranche 1 (25%)")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("send_realistic_test_plain")


# ---------------------------------------------------------------------------
# State I/O (mirrors remind_pending.py / collect_reply.py)
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


def _recipients() -> list[str]:
    return [p.strip() for p in ALERT_PHONE.split(",") if p.strip()]


def _short_id(signal_key: str, when: datetime) -> str:
    payload = f"{signal_key}|{when.isoformat()}".encode()
    return hashlib.sha256(payload).hexdigest()[:6]


def _drop_stale_sim_entries(data: dict) -> None:
    """Keep the state file from accumulating old sim/smoke pending rows.

    Leaves anything that isn't a previous run of this script alone —
    real signals, resolved tests, and orphan replies all stay put.
    """
    stale: list[str] = []
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if v.get("status") != "pending":
            continue
        sig = v.get("signal_key", "")
        is_sim  = sig.startswith("sim|") or sig.startswith("test|reminder-smoketest")
        is_old  = k in ("smoke1", "smoke-test")
        if is_sim or is_old:
            stale.append(k)
    for k in stale:
        log.info("dropping stale pending entry %s", k)
        data.pop(k, None)


# ---------------------------------------------------------------------------
# Send  (plain — no URL-bearing fields)
# ---------------------------------------------------------------------------

def _send(phone: str, message: str) -> bool:
    """Send via Textbelt using only phone + message + key (no webhook/URL fields)."""
    payload = {"phone": phone, "message": message, "key": TEXTBELT_KEY}
    try:
        r = httpx.post("https://textbelt.com/text", data=payload, timeout=15.0)
        body = r.json()
        log.info("textbelt[%s]: %s", phone, body)
        return bool(body.get("success"))
    except Exception as e:
        log.exception("textbelt call failed for %s: %s", phone, e)
        return False


def _build_message(code: str) -> str:
    """Build the SMS body.

    NOTE: We deliberately do NOT include the 6-char hex slug in the SMS
    body any more. Older versions of this script appended `Reply Y abc123
    / N abc123.` as an aid for matching the reply back to the pending
    record, but TextBelt's URL-content filter false-positives on short
    hex slugs and rejects unwhitelisted accounts. We still STORE the slug
    in pending state for internal tracking; collect_reply.find_pending_for_phone
    handles the bare-Y/bare-N case via "latest pending for this phone"
    correlation, which is unambiguous so long as we don't have multiple
    overlapping pending alerts for the same number.

    `code` is kept as a parameter for backwards-compatibility and so
    callers stay structurally identical.
    """
    total = SIM_LIVE * SIM_QUANTITY
    risk = int(round((abs(total) * 0.025 * 1.65) / 10) * 10)
    return (
        f"{SIM_COMMODITY.upper()}: Sell {SIM_QUANTITY:,} @ ${SIM_LIVE:.2f} "
        f"(${total:,.0f}). 1d risk -${risk:,.0f}. Reply Y or N."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    recipients = _recipients()
    if not TEXTBELT_KEY:
        log.error("TEXTBELT_KEY not set; aborting")
        return 1
    if not recipients:
        log.error("ALERT_PHONE not set; aborting")
        return 1
    now = datetime.now(timezone.utc)
    signal_key = (
        f"sim|{SIM_COMMODITY}|{SIM_CONTRACT}|SELL|{SIM_TARGET:.4f}|"
        f"{now.strftime('%Y-%m-%dT%H:%M:%S')}"
    )
    sid = _short_id(signal_key, now)
    message = _build_message(sid)
    log.info("message (%d chars): %s", len(message), message)
    # Send first; only stamp pending state if TextBelt accepted at least one
    # recipient. Otherwise we'd leave a ghost row for remind-pending to chase.
    any_ok = False
    for phone in recipients:
        if _send(phone, message):
            any_ok = True

    if not any_ok:
        log.error("no recipient accepted; not recording pending state")
        return 1

    data = _load()
    _drop_stale_sim_entries(data)
    data[sid] = {
        "signal_key": signal_key,
        "sent_at":    now.isoformat(),
        "message":    message,
        "status":     "pending",
        "recipients": {p: {"vote": None} for p in recipients},
    }
    _save(data)
    log.info("recorded pending sid=%s; remind-pending will nudge at +5 min",
             sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
