#!/usr/bin/env python3
"""
Quick local view of who has (and hasn't) replied to recent confirmation
prompts.

Usage:
    python3 scripts/who_replied.py            # latest 3 alerts, pretty
    python3 scripts/who_replied.py --all      # every alert in state
    python3 scripts/who_replied.py --pending  # only still-pending alerts
    python3 scripts/who_replied.py --sid a1b2c3   # one specific alert
    python3 scripts/who_replied.py --json     # raw JSON for piping

Reads `state/confirmations.json` (or a path passed via $CONF_FILE / a
positional argument). No network, no GitHub round-trip — just the file.

If you ran the test from your laptop, the freshest copy lives in the
GitHub repo (`jnimbles03/farm-trader`). Pull `main` first, or pass:
    python3 scripts/who_replied.py /path/to/confirmations.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# Default path matches collect_reply.py / remind_pending.py.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "state" / "confirmations.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last4(phone: str) -> str:
    """Reduce a +E.164 phone to its last 4 digits for terse display."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else phone


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _age(then: datetime | None, now: datetime) -> str:
    """Human-readable age: '2m ago', '5h ago', '3d ago'.

    If `then` is in the future relative to `now` (e.g. clock skew, or
    you're poking at synthetic data), we emit 'in <span>' so the output
    stays readable instead of leaking negative seconds.
    """
    if then is None:
        return "?"
    delta = now - then
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = -secs
        if secs < 60:    return f"in {secs}s"
        if secs < 3600:  return f"in {secs // 60}m"
        if secs < 86400: return f"in {secs // 3600}h"
        return f"in {secs // 86400}d"
    if secs < 60:    return f"{secs}s ago"
    if secs < 3600:  return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _vote_glyph(vote: str | None) -> str:
    """Single-character vote display, plus padding so columns align."""
    if vote == "Y":
        return "Y"
    if vote == "N":
        return "N"
    return "·"  # not voted yet


# ---------------------------------------------------------------------------
# Pretty-printer
# ---------------------------------------------------------------------------

def _print_alert(sid: str, entry: dict, now: datetime) -> None:
    sig    = entry.get("signal_key", "?")
    sent   = _parse_iso(entry.get("sent_at"))
    status = entry.get("status", "?")
    live   = entry.get("live_price")
    prior  = entry.get("prior_fired_price")

    # Header line — short_id, signal, status, age.
    age = _age(sent, now)
    price_tag = ""
    if isinstance(live, (int, float)):
        price_tag = f"  @ ${live:.2f}"
        if isinstance(prior, (int, float)):
            delta = live - prior
            sign  = "+" if delta >= 0 else ""
            price_tag += f" (prior ${prior:.2f}, {sign}${delta:.2f})"
    print(f"━━ {sid}  {sig}{price_tag}")
    print(f"   sent {age}   status: {status.upper()}")

    recipients = entry.get("recipients", {})
    if not recipients:
        print("   (no recipients on file)")
        print()
        return

    # Tally + per-recipient lines.
    yes = sum(1 for r in recipients.values() if r.get("vote") == "Y")
    no  = sum(1 for r in recipients.values() if r.get("vote") == "N")
    pending = sum(1 for r in recipients.values() if r.get("vote") is None)
    print(f"   {len(recipients)} recipient(s): {yes} Y, {no} N, {pending} pending")

    # Sort: pending first (so the people you're waiting on are at the
    # top), then by reply time.
    def sort_key(item):
        phone, r = item
        voted = r.get("vote")
        ts = _parse_iso(r.get("replied_at"))
        # pending → 0, voted → 1; then earliest reply first within voted.
        return (0 if voted is None else 1, ts or now)

    for phone, r in sorted(recipients.items(), key=sort_key):
        vote = _vote_glyph(r.get("vote"))
        when = _age(_parse_iso(r.get("replied_at")), now) if r.get("vote") else "—"
        raw  = r.get("raw") or ""
        raw_tag = f"  ({raw!r})" if raw else ""
        print(f"     [{vote}]  ...{_last4(phone)}   {when}{raw_tag}")
    print()


def _print_orphans(orphans: list, now: datetime) -> None:
    if not orphans:
        return
    print("━━ orphan replies (couldn't match to an alert)")
    for o in orphans[-10:]:  # last 10 is usually enough
        phone = _last4(o.get("phone", ""))
        text  = (o.get("text") or "").strip()
        when  = _age(_parse_iso(o.get("received_at")), now)
        reason = o.get("reason") or ""
        reason_tag = f"  [{reason}]" if reason else ""
        print(f"   ...{phone}  {when}  {text!r}{reason_tag}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("file", nargs="?",
                   default=os.environ.get("CONF_FILE", str(DEFAULT_FILE)),
                   help="Path to confirmations.json (default: state/confirmations.json)")
    p.add_argument("--all", action="store_true",
                   help="Show every alert, not just the latest 3.")
    p.add_argument("--pending", action="store_true",
                   help="Show only alerts whose status is still 'pending'.")
    p.add_argument("--sid", type=str, default=None,
                   help="Show just this one short_id.")
    p.add_argument("-n", "--limit", type=int, default=3,
                   help="How many recent alerts to show (default 3).")
    p.add_argument("--json", action="store_true",
                   help="Dump matching entries as JSON for piping.")
    args = p.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"error: {path} not found", file=sys.stderr)
        print("Tip: pull the latest 'main' branch of farm-trader, or pass "
              "the path to a confirmations.json explicitly.", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"error: {path} is not valid JSON: {e}", file=sys.stderr)
        return 1

    # Pull alerts (skip _orphans key) and sort newest first by sent_at.
    alerts = [
        (sid, entry) for sid, entry in data.items()
        if not sid.startswith("_") and isinstance(entry, dict)
    ]
    alerts.sort(key=lambda x: x[1].get("sent_at", ""), reverse=True)

    if args.sid:
        alerts = [(s, e) for s, e in alerts if s == args.sid]
        if not alerts:
            print(f"no alert with sid={args.sid}", file=sys.stderr)
            return 1
    elif args.pending:
        alerts = [(s, e) for s, e in alerts if e.get("status") == "pending"]
    elif not args.all:
        alerts = alerts[: max(1, args.limit)]

    if args.json:
        out = {sid: entry for sid, entry in alerts}
        if data.get("_orphans"):
            out["_orphans"] = data["_orphans"]
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    if not alerts:
        print("no alerts to show.")
        return 0

    now = datetime.now(timezone.utc)
    print(f"confirmations from {path}  (now {now.strftime('%Y-%m-%d %H:%M UTC')})")
    print()
    for sid, entry in alerts:
        _print_alert(sid, entry, now)

    if args.all or args.pending:
        _print_orphans(data.get("_orphans", []), now)

    return 0


if __name__ == "__main__":
    sys.exit(main())
