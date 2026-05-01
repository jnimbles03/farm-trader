#!/usr/bin/env python3
"""
Pulse confirmation status to the operator while alerts are still open.

While a confirmation is in `status: pending`, this script texts the
operator (OPERATOR_PHONE) every PULSE_INTERVAL seconds with a per-
respondent breakdown — who voted Y, who voted N, who's still out.

Differences from remind_pending.py:
  - Audience is the operator, not the recipient list.
  - We don't nag laggards here; reminders own that.
  - We skip the pulse if nothing has changed since the previous pulse,
    so the operator only gets pinged when there's news (or the first
    pulse for a freshly-fired alert).
  - Hard cap PULSE_MAX_COUNT pulses per alert so a never-resolving
    confirmation doesn't generate hundreds of texts overnight.

State is stored alongside the existing confirmations entry under
`pulses` so the script is stateless across runs:

    "a3f201": {
        ...
        "pulses": {
            "last_at":      "2026-05-01T17:25:00+00:00",
            "last_signature":"Y=2;N=0;P=1",
            "count":        3
        }
    }

Schedule:    every 5 minutes (cron: */5 ...) during market hours.
Idempotent:  yes. Re-running the same minute is a no-op once
             `last_at` is set.
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

# Required env
TEXTBELT_KEY   = os.environ.get("TEXTBELT_KEY", "")
OPERATOR_PHONE = os.environ.get("OPERATOR_PHONE", "+16302479950").strip()

# Tunables (env, with sane defaults)
PULSE_INTERVAL_S = int(os.environ.get("PULSE_INTERVAL_S",  "300"))   # 5 min
PULSE_MAX_COUNT  = int(os.environ.get("PULSE_MAX_COUNT",   "12"))    # ~1 hour
PULSE_QUIET_NEW  = int(os.environ.get("PULSE_QUIET_NEW_S", "60"))    # don't pulse alerts younger than 60s — let the original SMS land first

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("pulse_status")


# ---------------------------------------------------------------------------
# Same I/O helpers as remind_pending.py / collect_reply.py
# ---------------------------------------------------------------------------

def load_confirmations() -> dict:
    if not CONFIRMATIONS_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMATIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        log.error("confirmations.json corrupt, refusing to overwrite: %s", e)
        return {}


def save_confirmations(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    CONFIRMATIONS_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
    # Sanitized public copy — same shape as collect_reply.py writes.
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


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _last4(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else phone


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------

def _signature(entry: dict) -> str:
    """A compact fingerprint of vote state. If this hasn't changed
    since the last pulse, we skip — the operator already knows."""
    recipients = entry.get("recipients", {})
    yes = sum(1 for r in recipients.values() if r.get("vote") == "Y")
    no  = sum(1 for r in recipients.values() if r.get("vote") == "N")
    pending = sum(1 for r in recipients.values() if r.get("vote") is None)
    return f"Y={yes};N={no};P={pending}"


def _format_pulse(sid: str, entry: dict, now: datetime) -> str:
    """Single SMS body summarizing one pending alert."""
    sig = entry.get("signal_key", sid)
    sent = _parse_iso(entry.get("sent_at"))
    age_min = int((now - sent).total_seconds() // 60) if sent else 0

    recipients = entry.get("recipients", {})
    yes_l = []
    no_l  = []
    pend  = []
    for phone, r in recipients.items():
        tag = f"...{_last4(phone)}"
        v = r.get("vote")
        if v == "Y":
            yes_l.append(tag)
        elif v == "N":
            no_l.append(tag)
        else:
            pend.append(tag)

    parts = [f"PULSE {age_min}m: {sig}"]
    if yes_l:
        parts.append(f"Y: {', '.join(yes_l)}")
    if no_l:
        parts.append(f"N: {', '.join(no_l)}")
    if pend:
        parts.append(f"pending: {', '.join(pend)}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# SMS send (same shape as remind_pending.send_reminder)
# ---------------------------------------------------------------------------

def send_pulse(message: str) -> bool:
    if not (TEXTBELT_KEY and OPERATOR_PHONE):
        log.warning("pulse skipped — TEXTBELT_KEY or OPERATOR_PHONE missing")
        return False
    if not message.startswith("[FARM]"):
        message = "[FARM] " + message
    try:
        r = httpx.post(
            "https://textbelt.com/text",
            data={"phone":   OPERATOR_PHONE,
                  "message": message,
                  "key":     TEXTBELT_KEY},
            timeout=15.0,
        )
        body = r.json()
        ok = bool(body.get("success"))
        log.info("pulse[%s]: %s", OPERATOR_PHONE, body)
        return ok
    except Exception as e:
        log.exception("pulse send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def main() -> int:
    data = load_confirmations()
    if not data:
        log.info("no confirmations state — nothing to pulse")
        return 0

    now = datetime.now(timezone.utc)
    pulsed = 0
    skipped_unchanged = 0
    skipped_cap = 0
    skipped_quiet = 0

    for sid, entry in data.items():
        if sid.startswith("_") or not isinstance(entry, dict):
            continue
        if entry.get("status") != "pending":
            continue

        # Don't pulse alerts younger than PULSE_QUIET_NEW seconds — the
        # original SMS literally just went out, no point pinging.
        sent_at = _parse_iso(entry.get("sent_at"))
        if sent_at is None:
            log.warning("sid=%s has no parsable sent_at; skipping", sid)
            continue
        if (now - sent_at).total_seconds() < PULSE_QUIET_NEW:
            skipped_quiet += 1
            continue

        pulses = entry.setdefault("pulses", {})
        count   = int(pulses.get("count", 0))
        last_at = _parse_iso(pulses.get("last_at"))
        last_sig = pulses.get("last_signature", "")

        # Hard cap.
        if count >= PULSE_MAX_COUNT:
            skipped_cap += 1
            continue

        # Throttle to PULSE_INTERVAL_S between pulses (per alert).
        if last_at is not None:
            since = (now - last_at).total_seconds()
            if since < PULSE_INTERVAL_S:
                continue

        # Skip if state hasn't changed since the last pulse — operator
        # already knows. We DO send the very first pulse regardless,
        # since last_sig is empty and the current sig won't match.
        cur_sig = _signature(entry)
        if last_at is not None and cur_sig == last_sig:
            skipped_unchanged += 1
            continue

        msg = _format_pulse(sid, entry, now)
        if send_pulse(msg):
            pulses["last_at"]        = now.isoformat()
            pulses["last_signature"] = cur_sig
            pulses["count"]          = count + 1
            pulsed += 1
        else:
            log.warning("pulse failed for sid=%s; will retry next tick", sid)

    log.info("pulse sweep: sent=%d, unchanged=%d, at-cap=%d, too-new=%d",
             pulsed, skipped_unchanged, skipped_cap, skipped_quiet)

    # Always re-save so the public confirmations.json mirror stays
    # current and pulse bookkeeping persists.
    save_confirmations(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
