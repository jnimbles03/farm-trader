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


# Match "Y", "YES", "N", "NO" (case-insensitive) optionally followed by:
#   - a 6-char hex short_id (legacy / power-user path), OR
#   - a commodity hint ("corn" / "soy" / "soybeans" / "beans"), OR
#   - a 1-2 digit index ("1", "2") for "the Nth in the bounce list".
# Anything else falls through to the orphan bucket.
_VOTE_RE = re.compile(
    r"^\s*(y|yes|n|no)\b"
    r"(?:\s+(?:"
    r"(?P<sid>[a-f0-9]{6})"
    r"|(?P<commodity>corn|soybeans?|beans|soy)"
    r"|(?P<index>\d{1,2})"
    r"))?",
    re.IGNORECASE,
)


def parse_reply(text: str) -> tuple[str | None, dict]:
    """Return (vote, hint) where hint is a dict that may carry one of:
        {"sid": "<6hex>"}, {"commodity": "corn"|"soy"}, {"index": int}
    or be empty when the user just texted "Y" / "N"."""
    m = _VOTE_RE.match(text)
    if not m:
        return None, {}
    word = m.group(1).upper()
    vote = "Y" if word in ("Y", "YES") else "N"
    hint: dict = {}
    if m.group("sid"):
        hint["sid"] = m.group("sid").lower()
    elif m.group("commodity"):
        c = m.group("commodity").lower()
        hint["commodity"] = "soy" if c.startswith(("soy", "bean")) else c
    elif m.group("index"):
        try:
            hint["index"] = int(m.group("index"))
        except ValueError:
            pass
    return vote, hint


def pending_for_phone(data: dict, phone: str) -> list[tuple[str, str]]:
    """All pending (sid, sent_at) where this phone hasn't voted yet,
    newest first."""
    candidates = []
    for sid, entry in data.items():
        if sid.startswith("_"):
            continue
        if entry.get("status") != "pending":
            continue
        r = entry.get("recipients", {}).get(phone)
        if r is not None and r.get("vote") is None:
            candidates.append((sid, entry.get("sent_at", "")))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def find_pending_for_phone(data: dict, phone: str) -> str | None:
    """Latest pending entry where this phone hasn't voted yet.

    Used only when EXACTLY ONE alert is pending for the phone. If two
    or more are pending, the caller should refuse to guess and bounce
    a disambiguation SMS instead — see main().
    """
    cands = pending_for_phone(data, phone)
    return cands[0][0] if cands else None


def _send_bounce(phone: str, message: str) -> None:
    """Send a one-shot SMS back to the replying phone (no webhook).

    Used when an inbound Y/N is ambiguous and we need the user to
    re-send with the explicit code. We deliberately don't attach a
    replyWebhookUrl here so the user's *next* message routes through
    the normal /reply path with their corrected text.
    """
    if not (TEXTBELT_KEY and phone):
        log.warning("bounce SMS skipped — TEXTBELT_KEY or phone missing")
        return
    if not message.startswith("[FARM]"):
        message = "[FARM] " + message
    try:
        r = httpx.post(
            "https://textbelt.com/text",
            data={"phone": phone, "message": message, "key": TEXTBELT_KEY},
            timeout=15.0,
        )
        log.info("bounce[%s]: %s", phone, r.json())
    except Exception as e:
        log.exception("bounce send failed for %s: %s", phone, e)


def _describe_pending(entry: dict, sid: str) -> str:
    """Plain-English label for a pending alert: the signal_key + the
    price we showed at firing. No code shown — codes are an internal
    detail. Used inside the disambiguation bounce SMS."""
    sig  = entry.get("signal_key", sid)
    live = entry.get("live_price")
    if isinstance(live, (int, float)):
        return f"{sig} @ ${live:.2f}"
    return sig


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
    vote, hint = parse_reply(REPLY_TEXT)
    sid: str | None = hint.get("sid")

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
        cands = pending_for_phone(data, REPLY_PHONE)
        if not cands:
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

        # Try a commodity hint ("Y corn") against the pending list.
        if hint.get("commodity"):
            want = hint["commodity"]
            matches = [
                (s, sa) for s, sa in cands
                if (data[s].get("signal_key","").split("|")[:1] or [""])[0]
                   .lower() == want
            ]
            if len(matches) == 1:
                sid = matches[0][0]
                log.info("matched %s pending for %s by commodity=%s",
                         sid, REPLY_PHONE, want)
            elif len(matches) > 1:
                # Multiple of the same commodity — ask for an index.
                _send_bounce(
                    REPLY_PHONE,
                    f"Got '{vote} {want}' but you have {len(matches)} "
                    f"{want} alerts open. Reply '{vote} 1' or '{vote} 2' "
                    f"for the one you mean. " +
                    "; ".join(
                        f"{i+1}) {_describe_pending(data[s], s)}"
                        for i, (s, _) in enumerate(matches)
                    )
                )
                data.setdefault("_orphans", []).append({
                    "phone": REPLY_PHONE, "text": REPLY_TEXT,
                    "received_at": REPLY_RECEIVED_AT, "vote": vote,
                    "reason": "ambiguous_commodity",
                })
                save_confirmations(data)
                return 0
            else:
                # Commodity not in the pending list.
                _send_bounce(
                    REPLY_PHONE,
                    f"Got '{vote} {want}' but no {want} alert is open. "
                    f"Open: " +
                    "; ".join(
                        _describe_pending(data[s], s) for s, _ in cands
                    )
                )
                data.setdefault("_orphans", []).append({
                    "phone": REPLY_PHONE, "text": REPLY_TEXT,
                    "received_at": REPLY_RECEIVED_AT, "vote": vote,
                    "reason": "commodity_not_pending",
                })
                save_confirmations(data)
                return 0

        # Try a numeric-index hint ("Y 1") against the pending list.
        if sid is None and hint.get("index"):
            i = hint["index"]
            if 1 <= i <= len(cands):
                sid = cands[i - 1][0]
                log.info("matched %s pending for %s by index=%d",
                         sid, REPLY_PHONE, i)
            else:
                _send_bounce(
                    REPLY_PHONE,
                    f"Got '{vote} {i}' but only {len(cands)} alerts open. "
                    f"Reply '{vote} 1' through '{vote} {len(cands)}'."
                )
                data.setdefault("_orphans", []).append({
                    "phone": REPLY_PHONE, "text": REPLY_TEXT,
                    "received_at": REPLY_RECEIVED_AT, "vote": vote,
                    "reason": "index_out_of_range",
                })
                save_confirmations(data)
                return 0

        if sid is None and len(cands) > 1:
            # Two or more outstanding alerts for this phone and the
            # user replied with no code. Refuse to guess — historically
            # this routed to the newest, which is wrong half the time.
            # Bounce a disambiguation SMS that names every pending
            # alert by code + signal + price.
            log.warning(
                "ambiguous reply from %s: %d pending; bouncing for code",
                REPLY_PHONE, len(cands),
            )
            # Plain-English disambiguation: list the trades, ask which.
            # No codes — the user just texts back e.g. "Y corn" or
            # "N soy" and the next pass matches by commodity in the
            # signal_key. Falling back to "reply YES1 or YES2" if both
            # are the same commodity.
            same_commodity = (
                len({(data[s].get("signal_key","").split("|")[:1] or [""])[0]
                     for s, _ in cands}) == 1
            )
            if same_commodity:
                bounce = (
                    f"Got '{vote}' but you have {len(cands)} alerts open. "
                    f"Reply '{vote} 1' or '{vote} 2' for the trade you mean. "
                    + "; ".join(
                        f"{i+1}) {_describe_pending(data[s], s)}"
                        for i, (s, _) in enumerate(cands)
                    )
                )
            else:
                bounce = (
                    f"Got '{vote}' but you have {len(cands)} alerts open. "
                    f"Reply '{vote} corn' or '{vote} soy' for the one you "
                    f"mean. Open: "
                    + "; ".join(
                        _describe_pending(data[s], s) for s, _ in cands
                    )
                )
            _send_bounce(REPLY_PHONE, bounce)
            data.setdefault("_orphans", []).append({
                "phone":       REPLY_PHONE,
                "text":        REPLY_TEXT,
                "received_at": REPLY_RECEIVED_AT,
                "vote":        vote,
                "reason":      "ambiguous_no_sid",
                "pending_sids": [s for s, _ in cands],
            })
            save_confirmations(data)
            return 0
        # Single pending and no hint needed → use it.
        if sid is None:
            sid = cands[0][0]
            log.info("inferred short_id=%s for %s (only one pending)",
                     sid, REPLY_PHONE)

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
