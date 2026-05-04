"""
patch_advisor_bundle.py — overlay live Ritchie sidecar onto the published
advisor_context.json. Stripped-down alternative to context_builder.py for
the GHA runner, which does NOT have the books xlsx.

Reads:
  docs/advisor/advisor_context.json   (the canonical bundle, last hand-built locally)
  docs/advisor/ritchie_live.json      (the sidecar refresh_ritchie.py just wrote)

Writes:
  docs/advisor/advisor_context.json   (in place — appends a same-day storage_state row)

The merge logic mirrors advisor/context_builder.py::_read_ritchie_live so
local + GHA paths produce the same shape. Idempotent: if the latest
storage_state row is already from the same as_of date, we replace it
rather than append.

Exit codes:
  0  bundle updated (or no-op because sidecar absent / older than latest row)
  2  bundle file missing — refuse to write a partial
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BUNDLE = ROOT / "docs" / "advisor" / "advisor_context.json"
SIDECAR = ROOT / "docs" / "advisor" / "ritchie_live.json"


def merge_storage_state(storage: list[dict], live: dict) -> tuple[list[dict], bool]:
    """Return (new_storage_state, changed?). Same overlay logic as
    context_builder._read_ritchie_live, expressed against the JSON shape."""
    storage = list(storage or [])
    live_date = (live.get("as_of") or "")[:10]
    if not live_date:
        return storage, False

    last = storage[-1] if storage else {}
    last_date = last.get("as_of") or ""
    if live_date < last_date:
        # Sidecar older than xlsx history — don't go backwards.
        return storage, False

    storage_block = live.get("storage") or {}
    ap_block      = live.get("avg_pricing") or {}
    errs          = live.get("fetch_errors") or []
    note_bits = [
        f"Live from {live.get('source','bushel')} at {live.get('as_of')}",
        f"Account {live.get('account')}" if live.get("account") else "",
        "PC/FS values carried from prior xlsx row (not scraped).",
        f"Fetch warnings: {'; '.join(errs)}" if errs else "",
    ]
    new_row = {
        "as_of":            live_date,
        "corn_at_ritchie":  storage_block.get("corn_bu"),
        "beans_at_ritchie": storage_block.get("beans_bu"),
        "corn_at_pcfs":     last.get("corn_at_pcfs"),
        "beans_at_pcfs":    last.get("beans_at_pcfs"),
        "avg_pricing_corn": ap_block.get("corn_bu") if ap_block.get("corn_bu") is not None
                            else last.get("avg_pricing_corn"),
        "avg_pricing_soy":  ap_block.get("soy_bu")  if ap_block.get("soy_bu")  is not None
                            else last.get("avg_pricing_soy"),
        "notes":            " · ".join(b for b in note_bits if b),
    }
    if last_date == live_date and last.get("notes", "").startswith("Live from"):
        # Same-day re-run: replace rather than duplicate.
        storage[-1] = new_row
    else:
        storage.append(new_row)
    return storage, storage[-1] != last


def main() -> int:
    if not BUNDLE.exists():
        sys.stderr.write(f"!! bundle not present at {BUNDLE}; refusing to write\n")
        return 2
    if not SIDECAR.exists():
        sys.stderr.write(f"-- sidecar not present at {SIDECAR}; nothing to overlay\n")
        return 0

    bundle = json.loads(BUNDLE.read_text())
    try:
        live = json.loads(SIDECAR.read_text())
    except Exception as e:
        sys.stderr.write(f"!! sidecar unparseable ({e}); leaving bundle untouched\n")
        return 0

    storage = bundle.get("storage_state") or []
    new_storage, changed = merge_storage_state(storage, live)
    if not changed:
        sys.stderr.write("-- no change to storage_state\n")
        return 0
    bundle["storage_state"] = new_storage
    BUNDLE.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    sys.stderr.write(
        f"++ bundle updated: storage_state now ends with {new_storage[-1]['as_of']} "
        f"(corn={new_storage[-1]['corn_at_ritchie']}, "
        f"beans={new_storage[-1]['beans_at_ritchie']})\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
