#!/usr/bin/env python3
"""
freshness_check.py — daily canary for the Freis Farm data feeds.

Checks the most recent timestamp on each JSON file the dashboard reads.
If any are older than the configured threshold, fires an SMS to
ALERT_PHONE so silent workflow failures become loud failures.

Files checked (each with the timestamp field used):

  docs/bushel.json                    .fetchedAt
  docs/advisor/ritchie_live.json      .as_of
  docs/advisor/advisor_context.json   .last_refreshed_at
  docs/elevator_bids.json             .generated_at | .fetched_at | .updated_at
  docs/sales_log.json                 .generated_at

Threshold (hours): STALE_HOURS env var, default 24.

Sends via TextBelt to every comma-separated number in ALERT_PHONE,
using the same key (TEXTBELT_KEY) as the rest of the alerts pipeline.
Exit 0 if everything is fresh OR the SMS was sent successfully; exit 1
if an alert needed to fire and the send failed (the workflow itself
will then turn red, which is its own backup signal).

Usage:

    python3 scripts/freshness_check.py        # check + alert if stale
    python3 scripts/freshness_check.py --dry  # check + log, never send

Run from the farm-proxy repo root, or anywhere — the script auto-finds
the docs/ folder relative to its own location.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore  # we'll noop the SMS path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # farm-proxy/
DOCS = ROOT / "docs"

# (label, path, list of timestamp keys to try in order)
FEEDS = [
    ("bushel.json",          DOCS / "bushel.json",                      ["fetchedAt"]),
    ("ritchie_live.json",    DOCS / "advisor" / "ritchie_live.json",    ["as_of"]),
    ("advisor_context.json", DOCS / "advisor" / "advisor_context.json", ["last_refreshed_at", "generated_at"]),
    ("elevator_bids.json",   DOCS / "elevator_bids.json",               ["generated_at", "fetched_at", "updated_at"]),
    ("sales_log.json",       DOCS / "sales_log.json",                   ["generated_at"]),
]

STALE_HOURS = float(os.environ.get("STALE_HOURS", "24"))
TEXTBELT_KEY = os.environ.get("TEXTBELT_KEY", "")
ALERT_PHONE = os.environ.get("ALERT_PHONE", "")
TEXTBELT_URL = "https://textbelt.com/text"


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601-ish timestamp into a tz-aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Accept trailing 'Z'
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Some files store date-only (YYYY-MM-DD); treat as midnight UTC
    if len(s) == 10 and s.count("-") == 2:
        s = s + "T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def feed_age(path: Path, keys: list[str]) -> tuple[datetime | None, str]:
    """Return (timestamp, source_field) for the freshest field, or (None, '') on miss."""
    if not path.exists():
        return None, "<missing file>"
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        return None, f"<unparseable: {e}>"
    for key in keys:
        if key in data:
            ts = parse_iso(data.get(key))
            if ts is not None:
                return ts, key
    # As a last resort, fall back to filesystem mtime
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc), "<file mtime>"
    except Exception:
        return None, "<no timestamp>"


def hours_old(ts: datetime, now: datetime) -> float:
    return (now - ts).total_seconds() / 3600.0


def format_alert(stale: list[tuple[str, float, str]], now: datetime) -> str:
    """Build the SMS body. Stays inside one ~160-char segment when possible
    but we do NOT truncate detail (per Freis Farm reminder format pref)."""
    lines = [f"FREIS FARM data canary {now:%Y-%m-%d %H:%M UTC}"]
    lines.append(f"{len(stale)} feed(s) stale (>{int(STALE_HOURS)}h):")
    for label, age_h, source in stale:
        # Round to nearest hour or to 0.1h if young
        age_str = f"{age_h:.0f}h" if age_h >= 10 else f"{age_h:.1f}h"
        lines.append(f"- {label}: {age_str} ({source})")
    lines.append("Check Actions tab; refresh-bushel + build-advisor-context.")
    return "\n".join(lines)


def send_textbelt(message: str, recipients: list[str]) -> bool:
    if requests is None:
        print("warn: requests not available — cannot send SMS", file=sys.stderr)
        return False
    if not TEXTBELT_KEY:
        print("warn: TEXTBELT_KEY missing — cannot send SMS", file=sys.stderr)
        return False
    if not recipients:
        print("warn: no ALERT_PHONE recipients — cannot send SMS", file=sys.stderr)
        return False

    all_ok = True
    for phone in recipients:
        try:
            r = requests.post(
                TEXTBELT_URL,
                data={"phone": phone, "message": message, "key": TEXTBELT_KEY},
                timeout=15,
            )
            payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            success = bool(payload.get("success"))
            print(f"textbelt → {phone}: success={success} payload={payload}")
            if not success:
                all_ok = False
            time.sleep(0.4)  # gentle pacing if multiple recipients
        except Exception as e:
            print(f"textbelt → {phone}: ERROR {e}", file=sys.stderr)
            all_ok = False
    return all_ok


def main() -> int:
    dry_run = "--dry" in sys.argv
    now = datetime.now(timezone.utc)

    rows: list[tuple[str, datetime | None, str, float | None]] = []
    for label, path, keys in FEEDS:
        ts, source = feed_age(path, keys)
        age = hours_old(ts, now) if ts else None
        rows.append((label, ts, source, age))

    # Print a one-line summary per feed for the workflow log.
    print(f"freshness check {now:%Y-%m-%d %H:%M:%SZ} (threshold {STALE_HOURS}h)")
    for label, ts, source, age in rows:
        if ts is None:
            print(f"  [STALE]  {label:24} no timestamp ({source})")
        elif age is None:
            print(f"  [?]      {label:24} can't compute age")
        elif age > STALE_HOURS:
            print(f"  [STALE]  {label:24} {age:6.1f}h old (from {source})")
        else:
            print(f"  [ok]     {label:24} {age:6.1f}h old (from {source})")

    stale: list[tuple[str, float, str]] = [
        (label, (age if age is not None else 99999), (source or "<no ts>"))
        for label, ts, source, age in rows
        if (age is None or age > STALE_HOURS)
    ]

    if not stale:
        print("all feeds fresh ✓")
        return 0

    msg = format_alert(stale, now)
    print("--- alert message ---")
    print(msg)
    print("--- /alert ---")

    if dry_run:
        print("dry run — not sending SMS")
        return 0

    recipients = [p.strip() for p in ALERT_PHONE.split(",") if p.strip()]
    ok = send_textbelt(msg, recipients)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
