#!/usr/bin/env python3
"""
Refresh docs/news.json with current corn/soy market headlines.

Runs twice daily (morning + afternoon) via refresh-market-wire.yml.
Sources:
  1. Price-move lede items built from docs/prices.json
  2. Recent news articles from yfinance (ZC=F, ZS=F, ZW=F)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PRICES_FILE = DOCS / "prices.json"
NEWS_FILE   = DOCS / "news.json"
MAX_ITEMS   = 6
NEWS_MAX_AGE_DAYS = 3

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("refresh_market_wire")


def _dir(chg: float | None) -> str:
    if chg is None:
        return ""
    return "↑" if chg >= 0 else "↓"


def _tag(title: str, corn_dir: str, soy_dir: str) -> tuple[str, str]:
    """Return (tag_label, tag_class) inferred from article title."""
    t = title.lower()
    if any(w in t for w in ("corn", "maize", " zc")):
        return f"CORN {corn_dir}".strip(), "corn"
    if any(w in t for w in ("soy", "bean", " zs")):
        return f"SOY {soy_dir}".strip(), "soy"
    if any(w in t for w in ("dollar", "dxy", "fed ", "rate ", "inflation",
                             "crude", "energy", "macro", "export", "tariff")):
        return "MACRO", "macro"
    return "WATCH", "watch"


def load_prices() -> dict:
    try:
        return json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not read prices.json: %s", e)
        return {}


def main() -> int:
    now = datetime.now(timezone.utc)
    prices = load_prices()
    detail = prices.get("detail", {})
    corn_chg = detail.get("corn", {}).get("day_chg")
    soy_chg  = detail.get("soy",  {}).get("day_chg")
    corn_dir = _dir(corn_chg)
    soy_dir  = _dir(soy_chg)

    items: list[dict] = []

    # ── 1. Price-move lede items ────────────────────────────────────────────
    if prices.get("corn_dec") and corn_chg is not None:
        sign = "+" if corn_chg >= 0 else ""
        items.append({
            "id":           f"price-corn-{now.strftime('%Y%m%d-%H')}",
            "tag":          f"CORN {corn_dir}",
            "tag_class":    "corn",
            "text":         f"Dec corn ${prices['corn_dec']:.2f} ({sign}{corn_chg:.2f}¢ today).",
            "published_at": now.isoformat(),
        })
    if prices.get("soy_nov") and soy_chg is not None:
        sign = "+" if soy_chg >= 0 else ""
        items.append({
            "id":           f"price-soy-{now.strftime('%Y%m%d-%H')}",
            "tag":          f"SOY {soy_dir}",
            "tag_class":    "soy",
            "text":         f"Nov soy ${prices['soy_nov']:.2f} ({sign}{soy_chg:.2f}¢ today).",
            "published_at": now.isoformat(),
        })

    # ── 2. Yahoo Finance news ───────────────────────────────────────────────
    cutoff = now - timedelta(days=NEWS_MAX_AGE_DAYS)
    seen: set[str] = set()
    for sym in ("ZC=F", "ZS=F", "ZW=F"):
        try:
            news = yf.Ticker(sym).news or []
        except Exception as e:
            log.warning("yfinance %s failed: %s", sym, e)
            continue
        for art in news[:6]:
            uid   = str(art.get("uuid") or art.get("id") or "")
            title = (art.get("title") or "").strip()
            if not title or uid in seen:
                continue
            seen.add(uid or title)
            pub_ts = art.get("providerPublishTime")
            pub_dt = (datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                      if pub_ts else now)
            if pub_dt < cutoff:
                continue
            tag_label, tag_class = _tag(title, corn_dir, soy_dir)
            items.append({
                "id":           uid or f"yf-{sym}-{pub_ts}",
                "tag":          tag_label,
                "tag_class":    tag_class,
                "text":         title,
                "published_at": pub_dt.isoformat(),
            })

    # ── 3. Deduplicate, sort newest first, cap ──────────────────────────────
    deduped: list[dict] = []
    seen_ids: set[str] = set()
    for it in sorted(items, key=lambda x: x["published_at"], reverse=True):
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            deduped.append(it)
    deduped = deduped[:MAX_ITEMS]

    out = {"items": deduped, "generated_at": now.isoformat()}
    NEWS_FILE.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %d items → %s", len(deduped), NEWS_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
