#!/usr/bin/env python3
"""
Nudge recipients who haven't replied Y/N to a pending confirmation.

Called by .github/workflows/remind-pending.yml on a ~1-min cron. Sweeps
state/confirmations.json, finds entries still in `status: pending`, and
for each recipient with `vote: null` decides whether it's time to resend:

  - First reminder fires FIRST_REMINDER_DELAY seconds after sent_at
    (default 300 = 5 min).
  - Every reminder after that fires REMINDER_INTERVAL seconds after the
    previous reminder (default 60 = 1 min).
  - Stops after MAX_REMINDERS nudges per recipient (default 5) so a
    totally-ignored alert can't rack up a huge TextBelt bill.

Per-recipient bookkeeping is stamped back into confirmations.json:

  recipients[phone] = {
    "vote": null,
    "last_reminded_at": "2026-04-22T14:12:03+00:00",
    "reminders_sent":   3,
  }

Reminders re-attach REPLY_WEBHOOK_URL so a Y/N reply to the nudge still
round-trips through the worker → collect_reply.py flow. No short_id is
printed in the outbound SMS, same as the original alert — the collector
infers short_id from "most recent pending for this phone".

Exits 0 even when nothing was due. The commit step in the workflow
no-ops when there's no diff.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


ROOT               = Path(__file__).resolve().parent.parent
STATE_DIR          = ROOT / "state"
DOCS_DIR           = ROOT / "docs"
CONFIRMATIONS_FILE = STATE_DIR / "confirmations.json"
PUBLIC_CONF_FILE   = DOCS_DIR / "confirmations.json"

TEXTBELT_KEY      = os.environ.get("TEXTBELT_KEY", "")
REPLY_WEBHOOK_URL = os.environ.get("REPLY_WEBHOOK_URL", "")

FIRST_REMINDER_DELAY = int(os.environ.get("FIRST_REMINDER_DELAY", "300"))  # 5 min
REMINDER_INTERVAL    = int(os.environ.get("REMINDER_INTERVAL",    "60"))   # 1 min
MAX_REMINDERS        = int(os.environ.get("MAX_REMINDERS",        "5"))    # ~9 min total

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("remind_pending")


# ---------------------------------------------------------------------------
# State I/O (mirrors collect_reply.py — kept identical so both scripts agree
# on the sanitized public shape)
# ---------------------------------------------------------------------------

def load_confirmations() -> dict:
    if not CONFIRMATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMATIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("confirmations.json corrupt, refusing to overwrite: %s", e)
        sys.exit(1)


def save_confirmations(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    CONFIRMATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    sanitized: dict[str, dict] = {}
    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        recipients = entry.get("recipients", {})
        total = len(recipients)
        yes   = sum(1 for r in recipients.values() if r.get("vote") == "Y")
        no    = sum(1 for r in recipients.values() if r.get("vote") == "N")
        sanitized[sid] = {
            "signal_key": entry.get("signal_key"),
            "sent_at":    entry.get("sent_at"),
            "status":     entry.get("status"),
            "total":      total,
            "yes":        yes,
            "no":         no,
        }
    DOCS_DIR.mkdir(exist_ok=True)
    PUBLIC_CONF_FILE.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True) + "\n"
    )


# ---------------------------------------------------------------------------
# SMS send (standalone — evaluate.py's helpers aren't importable from here
# without pulling in yfinance/contract logic)
# ---------------------------------------------------------------------------

def send_reminder(phone: str, message: str) -> bool:
    if not TEXTBELT_KEY:
        log.warning("reminder skipped — TEXTBELT_KEY missing")
        return False
    try:
        data = {"phone": phone, "message": message, "key": TEXTBELT_KEY}
        if REPLY_WEBHOOK_URL:
            data["replyWebhookUrl"] = REPLY_WEBHOOK_URL
        r = httpx.post(
            "https://textbelt.com/text",
            data=data,
            timeout=15.0,
        )
        body = r.json()
        log.info("textbelt reminder[%s]: %s", phone, body)
        return bool(body.get("success"))
    except Exception as e:
        log.exception("textbelt reminder call failed for %s: %s", phone, e)
        return False


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # datetime.fromisoformat tolerates the "+00:00" suffix the other
        # scripts emit; it also handles the "Z" suffix on Python 3.11+.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _due(now: datetime, sent_at: datetime,
         last_reminded_at: datetime | None, reminders_sent: int) -> bool:
    if reminders_sent >= MAX_REMINDERS:
        return False
    if reminders_sent == 0:
        return now - sent_at >= timedelta(seconds=FIRST_REMINDER_DELAY)
    # Subsequent: use last reminder as the anchor.
    anchor = last_reminded_at or sent_at
    return now - anchor >= timedelta(seconds=REMINDER_INTERVAL)


def _reminder_message(entry: dict) -> str:
    """Context-carrying reminder text.

    The original alert already explained the what; the reminder just
    points at it and asks for a vote. We keep the full first line of
    the original so the trade details (contract, prices, bushels,
    tranche) survive — truncating mid-"Tranche" would be worse than
    letting the SMS run to 2 segments.

    Cap at 300 chars as a guardrail against a pathologically long
    original. Real alerts are ~115 chars; reminder wrapper adds ~38.
    """
    original = entry.get("message", "")
    snippet = original.strip().splitlines()[0] if original else ""
    if len(snippet) > 300:
        snippet = snippet[:297].rstrip() + "..."
    if snippet:
        return f"FREIS FARM reminder — still need Y/N: {snippet}"
    sig_key = entry.get("signal_key", "")
    return f"FREIS FARM reminder — still need Y/N on {sig_key}"


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def main() -> int:
    data = load_confirmations()
    if not data:
        log.info("no confirmations state — nothing to do")
        return 0

    now = datetime.now(timezone.utc)
    sent_count = 0
    skipped_cap = 0

    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        if entry.get("status") != "pending":
            continue

        sent_at = _parse_iso(entry.get("sent_at"))
        if sent_at is None:
            log.warning("sid=%s has no parsable sent_at; skipping", sid)
            continue

        message = _reminder_message(entry)
        recipients = entry.setdefault("recipients", {})

        for phone, r in recipients.items():
            if r.get("vote") is not None:
                continue  # already voted
            last_reminded_at = _parse_iso(r.get("last_reminded_at"))
            reminders_sent   = int(r.get("reminders_sent", 0))

            if reminders_sent >= MAX_REMINDERS:
                skipped_cap += 1
                continue
            if not _due(now, sent_at, last_reminded_at, reminders_sent):
                continue

            ok = send_reminder(phone, message)
            if ok:
                r["last_reminded_at"] = now.isoformat()
                r["reminders_sent"]   = reminders_sent + 1
                sent_count += 1
            else:
                # Don't bump the counter on failure — next tick will retry.
                log.warning("reminder to %s for sid=%s failed; will retry",
                            phone, sid)

    if sent_count or skipped_cap:
        log.info("sweep complete: sent=%d, skipped_at_cap=%d",
                 sent_count, skipped_cap)
    else:
        log.info("sweep complete: nothing due")

    # Always re-save — keeps the sanitized public copy in sync even on
    # no-op sweeps, and _save_confirmations is a no-commit no-op in the
    # GHA step if contents didn't change.
    save_confirmations(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
