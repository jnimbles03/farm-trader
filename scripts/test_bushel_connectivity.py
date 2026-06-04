#!/usr/bin/env python3
"""
Bushel Connectivity Health Check
Delegates to scrape_bushel_bids.py (the real scraper) so we test the
exact same auth flow that runs in production.

Exit 0 = live bids returned, all good
Exit 1 = any failure -> SMS alert fired
"""
import os
import re
import sys
import json
import subprocess
import requests
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scrape_bushel_bids.py"


def sanitize_for_sms(text: str) -> str:
    # Strip file paths so Textbelt unverified accounts dont reject on URL detection
    text = re.sub(r'File "[^"]+", line \d+', '[traceback]', text)
    # Remove http(s) URLs
    text = re.sub(r'https?://\S+', '[url]', text)
    # Collapse whitespace/newlines
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:280]


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


def main():
    if not SCRIPT.exists():
        msg = "[FARM] Bushel connectivity FAILED: scrape_bushel_bids.py not found"
        print(msg, file=sys.stderr)
        send_sms(msg)
        return 1

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--quiet"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=120,
    )

    stderr_clean = sanitize_for_sms(result.stderr.strip()[-600:]) if result.stderr.strip() else ""

    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            bid_count = len(data) if isinstance(data, list) else len(data.get("bids", []))
            print(f"OK: Bushel connectivity check passed. {bid_count} bids returned.")
            return 0
        except Exception as exc:
            reason = f"scraper exited 0 but output not valid JSON: {exc}"
    elif result.returncode == 2:
        reason = f"auth failure (exit 2). {stderr_clean}"
    elif result.returncode == 3:
        reason = f"no Ritchie bids found (exit 3). {stderr_clean}"
    else:
        reason = f"exit {result.returncode}. {stderr_clean}"

    msg = f"[FARM] Bushel connectivity FAILED: {reason}"[:280]
    print(msg, file=sys.stderr)
    send_sms(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
