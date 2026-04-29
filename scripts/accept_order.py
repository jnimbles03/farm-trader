#!/usr/bin/env python3
"""
Accept one OTP-verified draft order and append it to docs/orders.json.

Called by .github/workflows/accept-order.yml when the Cloudflare Worker
fires a repository_dispatch (event_type: order_draft). The worker has
already verified the SMS-OTP/HMAC bundle, so this script trusts the
payload and just persists it.

Inputs (env, passed from github.event.client_payload):
  ORDER_PHONE         the whitelisted phone that received the OTP
  ORDER_PAYLOAD_JSON  the order payload, JSON-encoded by toJSON()
  ORDER_ACCEPTED_AT   ISO timestamp when the worker accepted the OTP
  ORDER_NONCE         the per-submission nonce from /orders/start

Output: appends one row per tranche to docs/orders.json. Status is
always "draft" — drafts are inert until promoted to live, which is a
manual JSON edit (or a future "promote" UI). evaluate.py reads
docs/orders.json but does NOT fire SMS for draft rows.

Idempotency: keyed on payload.submission_id. If a row with the same
submission_id already exists, this script no-ops (so a workflow retry
or a duplicate dispatch doesn't double-write).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT       = Path(__file__).resolve().parent.parent
DOCS_DIR   = ROOT / "docs"
STATE_DIR  = ROOT / "state"
ORDERS_FILE = DOCS_DIR / "orders.json"   # public — dashboard reads this

# Order schema (one row per tranche):
# {
#   "id":            "<uuid>",
#   "submission_id": "<uuid from client>",
#   "crop":          "soy" | "corn",
#   "type":          "limit" | "market",
#   "bushels":       int,
#   "limit_price":   float | null,        # null when type=market
#   "expiry":        "YYYY-MM-DD" | null, # null when type=market
#   "status":        "draft",             # only state this script writes
#   "created_at":    "<iso>",
#   "accepted_at":   "<iso>",             # when worker verified OTP
#   "phone":         "+16302479950",      # for audit
#   "nonce":         "<otp nonce>",       # for audit
# }

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("accept_order")


def load_orders() -> dict[str, Any]:
    if not ORDERS_FILE.exists():
        return {"orders": []}
    try:
        data = json.loads(ORDERS_FILE.read_text())
        if not isinstance(data, dict) or "orders" not in data:
            log.warning("orders.json missing 'orders' key, resetting")
            return {"orders": []}
        return data
    except json.JSONDecodeError as e:
        log.error("orders.json corrupt, refusing to overwrite: %s", e)
        sys.exit(1)


def save_orders(data: dict[str, Any]) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    ORDERS_FILE.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def parse_payload() -> dict[str, Any]:
    raw = os.environ.get("ORDER_PAYLOAD_JSON", "").strip()
    if not raw:
        log.error("ORDER_PAYLOAD_JSON missing")
        sys.exit(1)
    # `toJSON()` in GitHub Actions stringifies the object — it lands as a
    # JSON string. Parse it once.
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("ORDER_PAYLOAD_JSON parse failed: %s\nraw=%r", e, raw[:500])
        sys.exit(1)


def validate_payload(p: dict[str, Any]) -> None:
    required = ("submission_id", "crop", "tranches")
    for k in required:
        if k not in p:
            log.error("payload missing %s", k)
            sys.exit(1)
    if p["crop"] not in ("soy", "corn"):
        log.error("payload.crop must be 'soy' or 'corn', got %r", p["crop"])
        sys.exit(1)
    if not isinstance(p["tranches"], list) or not p["tranches"]:
        log.error("payload.tranches must be a non-empty list")
        sys.exit(1)
    for i, t in enumerate(p["tranches"]):
        if not isinstance(t, dict):
            log.error("tranche %d not an object", i)
            sys.exit(1)
        if t.get("type") not in ("limit", "market"):
            log.error("tranche %d type must be limit|market", i)
            sys.exit(1)
        if not isinstance(t.get("bushels"), (int, float)) or t["bushels"] <= 0:
            log.error("tranche %d bushels must be > 0", i)
            sys.exit(1)
        if t["type"] == "limit":
            if not isinstance(t.get("limit_price"), (int, float)) or t["limit_price"] <= 0:
                log.error("tranche %d limit_price must be > 0 when type=limit", i)
                sys.exit(1)
            if not isinstance(t.get("expiry"), str) or len(t["expiry"]) != 10:
                log.error("tranche %d expiry must be YYYY-MM-DD when type=limit", i)
                sys.exit(1)


def main() -> int:
    phone        = os.environ.get("ORDER_PHONE", "").strip()
    accepted_at  = os.environ.get("ORDER_ACCEPTED_AT", "").strip() \
                   or datetime.now(timezone.utc).isoformat()
    nonce        = os.environ.get("ORDER_NONCE", "").strip()

    payload = parse_payload()
    validate_payload(payload)

    sid = payload["submission_id"]
    data = load_orders()
    existing_sids = {row.get("submission_id") for row in data["orders"]}
    if sid in existing_sids:
        log.info("submission_id %s already accepted, skipping", sid)
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    crop = payload["crop"]
    new_rows: list[dict[str, Any]] = []
    for t in payload["tranches"]:
        is_market = t["type"] == "market"
        new_rows.append({
            "id":            str(uuid.uuid4()),
            "submission_id": sid,
            "crop":          crop,
            "type":          t["type"],
            "bushels":       int(t["bushels"]),
            "limit_price":   None if is_market else float(t["limit_price"]),
            "expiry":        None if is_market else t["expiry"],
            "status":        "draft",
            "created_at":    payload.get("submitted_at") or now_iso,
            "accepted_at":   accepted_at,
            "phone":         phone,
            "nonce":         nonce,
        })

    data["orders"].extend(new_rows)
    save_orders(data)
    log.info("appended %d draft row(s) for submission_id=%s",
             len(new_rows), sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
