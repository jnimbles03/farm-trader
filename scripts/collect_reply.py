#!/usr/bin/env python3
"""
Collect one inbound SMS reply and update confirmation state.

Called by .github/workflows/collect-reply.yml when the Cloudflare Worker
fires a repository_dispatch event. Reads reply details from env vars
(GHA passes them from github.event.client_payload), updates
state/confirmations.json, and — on terminal transitions — sends a
follow-up SMS summarizing the vote.

Confirmation state shape (state/confirmations.json):

{
  "a1b2c3": {
    "signal_key": "corn|2026-12|SELL|4.7000",
    "sent_at":    "2026-04-21T14:30:12+00:00",
    "message":    "FREIS FARM SELL target hit — ...",
    "status":     "pending" | "confirmed" | "vetoed",
    "recipients": {
      "+13125551234": {"vote": "Y", "replied_at": "...", "raw": "Y a1b2c3"},
      "+13125559876": {"vote": null}
    }
  },
  "_orphans": [
    {"phone": "+1...", "text": "yep!", "received_at": "...", "vote": "Y"}
  ]
}

A sanitized copy (no phone numbers, just counts) lands in
docs/confirmations.json for the dashboard to consume.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


ROOT                = Path(__file__).resolve().parent.parent
STATE_DIR           = ROOT / "state"
DOCS_DIR            = ROOT / "docs"
CONFIRMATIONS_FILE  = STATE_DIR / "confirmations.json"
PUBLIC_CONF_FILE    = DOCS_DIR / "confirmations.json"

REPLY_PHONE       = os.environ.get("REPLY_PHONE", "").strip()
REPLY_TEXT        = os.environ.get("REPLY_TEXT", "").strip()
REPLY_RECEIVED_AT = os.environ.get("REPLY_RECEIVED_AT", "").strip()
TEXTBELT_KEY      = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE       = os.environ.get("ALERT_PHONE", "")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("collect_reply")


def load_confirmations() -> dict:
    if not CONFIRMATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMATIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("confirmations.json corrupt, resetting: %s", e)
        return {}


def save_confirmations(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    CONFIRMATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    # Public, sanitized copy — phone numbers stripped, just tally per sid
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


# Match "Y", "YES", "N", "NO" (case-insensitive) optionally followed by a
# 6-char hex short_id. Anything else falls through to the orphan bucket.
_VOTE_RE = re.compile(r"^\s*(y|yes|n|no)\b\s*([a-f0-9]{6})?", re.IGNORECASE)


def parse_reply(text: str) -> tuple[str | None, str | None]:
    m = _VOTE_RE.match(text)
    if not m:
        return None, None
    word = m.group(1).upper()
    vote = "Y" if word in ("Y", "YES") else "N"
    sid = m.group(2).lower() if m.group(2) else None
    return vote, sid


def find_pending_for_phone(data: dict, phone: str) -> str | None:
    """Latest pending entry where this phone hasn't voted yet.

    Used as a fallback when the recipient replies with just "Y" and no
    short_id — we assume they mean the most recent outstanding prompt.
    """
    candidates = []
    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        if entry.get("status") != "pending":
            continue
        r = entry.get("recipients", {}).get(phone)
        if r is not None and r.get("vote") is None:
            candidates.append((sid, entry.get("sent_at", "")))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def send_follow_up(phones: list[str], message: str) -> None:
    """Fan a single group notification out via TextBelt (no replyWebhookUrl).

    We intentionally don't attach a webhook here — this is a terminal
    notification, not another prompt. Replies to it land in the orphan
    bucket, which is fine.
    """
    if not TEXTBELT_KEY:
        log.warning("follow-up SMS skipped — TEXTBELT_KEY missing")
        return
    if not phones:
        log.warning("follow-up SMS skipped — no recipients")
        return
    if not message.startswith("[FARM]"):
        message = "[FARM] " + message
    for phone in phones:
        try:
            r = httpx.post(
                "https://textbelt.com/text",
                data={"phone": phone, "message": message, "key": TEXTBELT_KEY},
                timeout=15.0,
            )
            log.info("follow-up[%s]: %s", phone, r.json())
        except Exception as e:
            log.exception("follow-up failed for %s: %s", phone, e)


def main() -> int:
    if not (REPLY_PHONE and REPLY_TEXT):
        log.error("missing REPLY_PHONE or REPLY_TEXT")
        return 1

    log.info("reply from %s: %r (received_at=%s)",
             REPLY_PHONE, REPLY_TEXT, REPLY_RECEIVED_AT)

    data = load_confirmations()
    vote, sid = parse_reply(REPLY_TEXT)

    if vote is None:
        log.info("unrecognized reply; stashing in _orphans")
        data.setdefault("_orphans", []).append({
            "phone":       REPLY_PHONE,
            "text":        REPLY_TEXT,
            "received_at": REPLY_RECEIVED_AT,
        })
        save_confirmations(data)
        return 0

    if sid is None:
        sid = find_pending_for_phone(data, REPLY_PHONE)
        if sid is None:
            log.info("no pending prompt for %s; stashing in _orphans",
                     REPLY_PHONE)
            data.setdefault("_orphans", []).append({
                "phone":       REPLY_PHONE,
                "text":        REPLY_TEXT,
                "received_at": REPLY_RECEIVED_AT,
                "vote":        vote,
            })
            save_confirmations(data)
            return 0
        log.info("inferred short_id=%s for %s", sid, REPLY_PHONE)

    entry = data.get(sid)
    if not entry:
        log.warning("unknown short_id %s — stashing reply in _orphans", sid)
        data.setdefault("_orphans", []).append({
            "phone":       REPLY_PHONE,
            "text":        REPLY_TEXT,
            "received_at": REPLY_RECEIVED_AT,
            "vote":        vote,
            "short_id":    sid,
        })
        save_confirmations(data)
        return 0

    recipients = entry.setdefault("recipients", {})
    if REPLY_PHONE not in recipients:
        # Reply from a number that wasn't in the original recipient list
        # (e.g., ALERT_PHONE was edited after the alert went out). Record
        # it anyway so nothing is silently dropped.
        log.warning("phone %s wasn't in recipient list for %s — recording",
                    REPLY_PHONE, sid)
        recipients[REPLY_PHONE] = {}
    recipients[REPLY_PHONE]["vote"]       = vote
    recipients[REPLY_PHONE]["replied_at"] = (
        REPLY_RECEIVED_AT or datetime.now(timezone.utc).isoformat()
    )
    recipients[REPLY_PHONE]["raw"]        = REPLY_TEXT

    # Aggregate status:
    #   any "N"         → vetoed (terminal)
    #   all "Y"         → confirmed (terminal)
    #   otherwise       → pending (waiting on more replies)
    prior_status = entry.get("status", "pending")
    votes = [r.get("vote") for r in recipients.values()]
    if "N" in votes:
        entry["status"] = "vetoed"
    elif votes and all(v == "Y" for v in votes):
        entry["status"] = "confirmed"
    else:
        entry["status"] = "pending"

    save_confirmations(data)
    log.info("short_id=%s status %s → %s",
             sid, prior_status, entry["status"])

    # Fire the group follow-up only when we JUST transitioned to terminal.
    if prior_status != entry["status"] and entry["status"] in (
        "confirmed", "vetoed"
    ):
        sig_key = entry.get("signal_key", sid)
        phones  = [p.strip() for p in ALERT_PHONE.split(",") if p.strip()]
        if entry["status"] == "confirmed":
            send_follow_up(
                phones,
                f"FREIS FARM OK  All confirmed — {sig_key}",
            )
        else:
            vetoers = [
                p for p, r in recipients.items() if r.get("vote") == "N"
            ]
            send_follow_up(
                phones,
                f"FREIS FARM X  Vetoed by {', '.join(vetoers)} — {sig_key}",
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
