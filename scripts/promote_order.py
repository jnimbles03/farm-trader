"""
Promote a draft order to live status.

Usage: python scripts/promote_order.py <order_id>

Flips status draft → live and writes a promoted_at timestamp.
The promote-order.yml workflow calls this before submit_offer.py.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ORDERS_PATH = Path(__file__).parent.parent / "docs" / "orders.json"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: promote_order.py <order_id>")
        sys.exit(1)

    order_id = sys.argv[1].strip()
    data = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))

    matched = False
    for order in data.get("orders", []):
        if order["id"] == order_id:
            if order.get("status") != "draft":
                print(f"Order {order_id!r} is already {order['status']!r} — nothing to do.")
                return
            order["status"] = "live"
            order["promoted_at"] = datetime.now(timezone.utc).isoformat()
            matched = True
            print(
                f"Promoted {order_id}: "
                f"{order['bushels']} bu {order['crop']} @ ${order['limit_price']} "
                f"expiry {order.get('expiry', '—')}"
            )

    if not matched:
        print(f"Order {order_id!r} not found in orders.json")
        sys.exit(1)

    ORDERS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
