"""USDA report alert pair — preview (D-1) + release (T+5 min).

Generates the two SMS messages we send around scheduled USDA reports
(WASDE, Crop Production, Grain Stocks, Acreage, Prospective Plantings).

Driven by `usda_reports` in plan.json. Each report entry holds:

  {
    "report_id": "wasde-2026-05",        // unique
    "label":     "May WASDE",
    "release":   "2026-05-12T11:00-05:00",
    "fields":    [                       // what the report prints
      { "key": "us_corn_yield_bu_ac", "label": "US corn yield (bu/ac)",
        "asset": "corn", "kind": "yield" },
      { "key": "us_corn_ending_stocks_mb", "label": "US corn ending stocks (mb)",
        "asset": "corn", "kind": "ending_stocks" },
      ...
    ],
    "trade_estimates": {                 // consensus from Reuters / Pro Farmer
      "us_corn_yield_bu_ac":          181.0,
      "us_corn_ending_stocks_mb":    1825,
      ...
    },
    "actuals": {                         // filled when report releases
      "us_corn_yield_bu_ac":          179.8,
      "us_corn_ending_stocks_mb":    1750
    },
    "cftc": {                            // optional, for squeeze adjustment
      "corn_net_spec_z": 0.4,
      "soy_net_spec_z":  1.7
    }
  }

Two CLI commands:

  python usda_alerts.py preview <report_id>      # fires D-1 evening
  python usda_alerts.py release <report_id>      # fires within 15 min of release

Both print the SMS body to stdout. evaluate.py wires them into the
existing send_news_alerts() pipeline by emitting news items with
{"format": "usda-pair", "body": <text>}.

Magnitude model (calibrated against the 2026-04-28 backtest):

  Corn:  each 1% surprise on production/yield/stocks → 1.5-2.5% same-session
         (i.e., ~7-12¢/bu at $4.50). Direction is opposite: bigger crop = lower price.
  Soy:   each 1% surprise on stocks → 1.0-1.7% same-session (~10-18¢ at $10.50).
         Yield surprises matter less for old-crop soy unless huge.
  CFTC:  if net-spec |z| > 1.5, multiply the upper end of the band by 1.5x — that's
         the long-squeeze / short-cover amplification we under-called on
         Trump-Xi soy (-6.5% limit-down vs predicted -2.5 to -5%).

Confidence:
  H — surprise is unambiguous (>1.5%) AND specs near neutral.
  M — surprise marginal OR specs >1.5σ from neutral.
  L — surprise marginal AND offsetting story already digesting.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLAN_FILE = Path(__file__).parent / "docs" / "plan.json"

# Per-asset multipliers on the *dominant* supply-side surprise (in %).
# A 1% production/yield surprise on corn produces a 1.5-2.5% same-session
# corn move. Tuned against the Jan 12 2026 WASDE backtest (+2.9% corn-
# production surprise → -5.8% corn close; ratio ~2.0, mid-band).
#
# Priority among supply fields: yield > production > plantings > stocks.
# Stocks is a derived/leveraged field — a 10% stocks miss may correspond
# to a 1-2% production miss, so we discount stocks-only surprises by 0.3.
IMPACT = {
    "corn": {
        "self":     (1.5, 2.5),   # 1% supply surprise → 1.5-2.5% corn move
        "soy_xfer": (0.4, 0.8),   # corn surprise → soy sympathy band
    },
    "soy": {
        "self":      (1.0, 1.7),
        "corn_xfer": (0.3, 0.6),
    },
}

SUPPLY_PRIORITY = ("yield", "production", "plantings", "harvested_acres",
                   "ending_stocks")
STOCKS_DISCOUNT = 0.3   # if only a stocks field is available, shrink it

# CFTC squeeze multiplier on the upper end of the magnitude band.
SQUEEZE_Z_THRESHOLD = 1.5
SQUEEZE_MULT = 1.5

# Surprise threshold: below this, we don't fire (matches the medium
# threshold from the alert spec).
MATERIAL_SURPRISE_PCT = 0.5  # 0.5% surprise on a key field


def _load_plan() -> dict:
    with open(PLAN_FILE) as fh:
        return json.load(fh)


def _find_report(plan: dict, report_id: str) -> dict | None:
    for r in plan.get("usda_reports", []):
        if r.get("report_id") == report_id:
            return r
    return None


def _direction_word(surprise_pct: float, is_supply_field: bool) -> str:
    """Bigger supply (yield/stocks/plantings up) is bearish for price.
    Demand surprises (exports up) are bullish — set is_supply_field=False
    for those."""
    if is_supply_field:
        return "down" if surprise_pct > 0 else "up"
    return "up" if surprise_pct > 0 else "down"


def compute_surprise(report: dict) -> list[dict]:
    """Return per-field surprise list. Empty if no actuals yet."""
    estimates = report.get("trade_estimates", {})
    actuals   = report.get("actuals", {})
    if not actuals:
        return []
    out = []
    for field in report.get("fields", []):
        key = field["key"]
        est = estimates.get(key)
        act = actuals.get(key)
        if est is None or act is None or est == 0:
            continue
        delta_pct = (act - est) / abs(est) * 100.0
        out.append({
            **field,
            "estimate": est,
            "actual":   act,
            "delta":    act - est,
            "pct":      delta_pct,
        })
    return out


def _dominant_surprise(surprises: list[dict], asset: str) -> dict | None:
    """Pick the single surprise that should drive the prediction for this
    asset. The market reacts to whichever production-related field carries
    the biggest miss — so we pick the largest |%| among production/yield/
    plantings/acres, falling back to stocks (discounted) if no headline
    field surprised. Returns None if no material surprise."""
    primary = [s for s in surprises
               if s.get("asset") == asset
               and s.get("kind") in ("yield", "production", "plantings",
                                     "harvested_acres")
               and abs(s["pct"]) >= MATERIAL_SURPRISE_PCT]
    if primary:
        return max(primary, key=lambda s: abs(s["pct"]))
    stocks = [s for s in surprises
              if s.get("asset") == asset and s.get("kind") == "ending_stocks"
              and abs(s["pct"]) >= MATERIAL_SURPRISE_PCT]
    if stocks:
        return max(stocks, key=lambda s: abs(s["pct"]))
    return None


def predict_move(report: dict, surprises: list[dict]) -> dict:
    """Build corn + soy predicted move bands from the dominant supply
    surprise per asset, plus cross-asset sympathy. Pcts in the band are
    fractions of price (e.g., 0.058 = 5.8%). Sign is +/-/±."""
    corn_z = report.get("cftc", {}).get("corn_net_spec_z", 0.0)
    soy_z  = report.get("cftc", {}).get("soy_net_spec_z",  0.0)

    def _asset_band(asset: str) -> tuple[float, float, str]:
        own = _dominant_surprise(surprises, asset)
        other_asset = "soy" if asset == "corn" else "corn"
        cross = _dominant_surprise(surprises, other_asset)

        lo = hi = 0.0
        sign_total = 0

        if own:
            mag = abs(own["pct"]) / 100.0
            if own["kind"] == "ending_stocks":
                mag *= STOCKS_DISCOUNT
            mlo, mhi = IMPACT[asset]["self"]
            lo += mag * mlo
            hi += mag * mhi
            # supply surprise +X% → price -X%
            sign_total += -1 if own["pct"] > 0 else 1

        if cross:
            mag = abs(cross["pct"]) / 100.0
            if cross["kind"] == "ending_stocks":
                mag *= STOCKS_DISCOUNT
            # Cross-asset: a surprise on other_asset produces sympathy in asset.
            xfer_key = f"{asset}_xfer"
            mlo, mhi = IMPACT[other_asset][xfer_key]
            lo += mag * mlo
            hi += mag * mhi
            sign_total += -1 if cross["pct"] > 0 else 1

        # CFTC squeeze: widen upper end when funds crowded.
        z = corn_z if asset == "corn" else soy_z
        if abs(z) >= SQUEEZE_Z_THRESHOLD:
            hi *= SQUEEZE_MULT

        if sign_total > 0:
            sign = "+"
        elif sign_total < 0:
            sign = "-"
        else:
            sign = "±"
        return (lo, hi, sign)

    return {
        "corn": _asset_band("corn"),
        "soy":  _asset_band("soy"),
    }


def _confidence(surprises: list[dict], cftc: dict) -> tuple[str, str]:
    """Return (H/M/L, one-line falsifier)."""
    if not surprises:
        return ("L", "no actuals yet")
    biggest = max(abs(s["pct"]) for s in surprises)
    crowded = any(abs(cftc.get(k, 0.0)) >= SQUEEZE_Z_THRESHOLD
                  for k in ("corn_net_spec_z", "soy_net_spec_z"))
    if biggest >= 1.5 and not crowded:
        return ("H", "magnitude depends on positioning")
    if biggest >= 1.5 and crowded:
        return ("M", "specs crowded — squeeze can amplify")
    if biggest >= 0.5:
        return ("M", "marginal surprise")
    return ("L", "below material threshold")


def _ref_price(plan: dict, asset: str) -> float:
    """Pull a reference price for ¢/bu translation. Falls back to plan
    commodity oct_low if no live price is wired in."""
    c = plan.get("commodities", {}).get(
        "corn" if asset == "corn" else "soybean", {})
    return float(c.get("oct_low", 4.50 if asset == "corn" else 10.50))


def _fmt_band(asset: str, band: tuple, ref_price: float) -> str:
    lo, hi, sign = band
    cents_lo = lo * ref_price * 100
    cents_hi = hi * ref_price * 100
    pct_lo = lo * 100
    pct_hi = hi * 100
    if sign == "-":
        return f"{asset} -{cents_lo:.0f} to -{cents_hi:.0f}¢ (-{pct_lo:.1f} to -{pct_hi:.1f}%)"
    if sign == "+":
        return f"{asset} +{cents_lo:.0f} to +{cents_hi:.0f}¢ (+{pct_lo:.1f} to +{pct_hi:.1f}%)"
    return f"{asset} ±{cents_lo:.0f}-{cents_hi:.0f}¢ (±{pct_lo:.1f}-{pct_hi:.1f}%)"


def format_preview_sms(report: dict, plan: dict) -> str:
    """D-1 preview: list trade estimates and key things to watch."""
    label = report.get("label", "USDA report")
    release = report.get("release", "")
    estimates = report.get("trade_estimates", {})
    fields = report.get("fields", [])

    when = ""
    if release:
        try:
            dt = datetime.fromisoformat(release.replace("Z", "+00:00"))
            when = dt.strftime("%a %b %-d, %-I:%M%p %Z").strip()
        except ValueError:
            when = release

    est_lines = []
    for f in fields:
        v = estimates.get(f["key"])
        if v is None:
            continue
        est_lines.append(f"{f['label']}: est {v}")

    # [FARM] prefix is added centrally in evaluate.py _send_sms_textbelt;
    # don't repeat any sub-tag here.
    body = (
        f"{label} preview — {when}\n"
        f"WHY: surprise vs trade estimate is what moves the market\n"
        f"WATCH: " + "; ".join(est_lines) + "\n"
        f"Alert at release with surprise + predicted move.\n"
        f"INFO ONLY — do not reply."
    )
    return body


def format_release_sms(report: dict, plan: dict) -> str:
    """T+5 release: surprise vs estimate + predicted move band."""
    label = report.get("label", "USDA report")
    surprises = compute_surprise(report)

    if not surprises:
        return (
            f"{label}: actuals not yet loaded.\n"
            f"Re-run when plan.json is updated with the release numbers."
        )

    # One-line surprise summary (top 2 by magnitude)
    surprises_sorted = sorted(surprises, key=lambda s: abs(s["pct"]), reverse=True)
    top = surprises_sorted[:2]
    sur_str = "; ".join(
        f"{s['label']} {s['actual']:g} vs est {s['estimate']:g} "
        f"({'+' if s['pct'] > 0 else ''}{s['pct']:.1f}%)"
        for s in top
    )

    # Predicted move
    bands = predict_move(report, surprises)
    corn_p = _ref_price(plan, "corn")
    soy_p  = _ref_price(plan, "soy")
    move_corn = _fmt_band("corn", bands["corn"], corn_p)
    move_soy  = _fmt_band("soy",  bands["soy"],  soy_p)

    # Confidence
    conf, falsifier = _confidence(surprises, report.get("cftc", {}))

    # Material gate: if both bands are below 1¢ predicted, skip
    if bands["corn"][1] * corn_p * 100 < 1 and bands["soy"][1] * soy_p * 100 < 1:
        return ""  # caller checks for empty and skips fire

    body = (
        f"{label}: {sur_str}\n"
        f"WHY: {'bearish supply surprise' if any(s['pct']>0 and s.get('kind') in ('yield','ending_stocks','production') for s in top) else 'bullish supply surprise' if any(s['pct']<0 and s.get('kind') in ('yield','ending_stocks','production') for s in top) else 'demand-side shift'}, next 1-2 sessions\n"
        f"EXPECT: {move_corn} / {move_soy}\n"
        f"CONF: {conf} — {falsifier}\n"
        f"INFO ONLY — do not reply."
    )
    return body


def usda_pair_news_for_today(plan: dict | None = None,
                             now: datetime | None = None) -> list[dict]:
    """Return news-pipeline items for any USDA report whose preview or
    release window matches today. Items have format='usda-pair' so the
    SMS formatter knows to use the prebuilt body verbatim."""
    plan = plan or _load_plan()
    now = now or datetime.now(timezone.utc)
    out = []
    for report in plan.get("usda_reports", []):
        rid = report.get("report_id")
        if not rid:
            continue
        release = report.get("release")
        if not release:
            continue
        try:
            rel_dt = datetime.fromisoformat(release.replace("Z", "+00:00"))
        except ValueError:
            continue

        # Preview: fire on D-1 (calendar-day before release in release tz)
        days_to = (rel_dt.date() - now.date()).days
        if days_to == 1 and report.get("trade_estimates"):
            body = format_preview_sms(report, plan)
            out.append({
                "id":      f"{rid}-preview",
                "date":    now.strftime("%Y-%m-%d"),
                "title":   f"{report['label']} preview",
                "impact":  "L",   # always SMS-tier
                "affects": "both",
                "source":  "USDA",
                "format":  "usda-pair",
                "phase":   "preview",   # routes to TRADE_PHONES (narrow)
                "body":    body,
            })

        # Release: fire on D when actuals are present
        if days_to == 0 and report.get("actuals"):
            body = format_release_sms(report, plan)
            if body:  # empty = below-threshold surprise, suppress
                out.append({
                    "id":      f"{rid}-release",
                    "date":    now.strftime("%Y-%m-%d"),
                    "title":   f"{report['label']} release",
                    "impact":  "L",
                    "affects": "both",
                    "source":  "USDA",
                    "format":  "usda-pair",
                    "phase":   "release",   # routes to NEWS_PHONES (wider)
                    "body":    body,
                })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["preview", "release", "today"])
    p.add_argument("report_id", nargs="?",
                   help="report_id from plan.json usda_reports[]; "
                        "omit with mode=today")
    args = p.parse_args()

    plan = _load_plan()

    if args.mode == "today":
        items = usda_pair_news_for_today(plan)
        if not items:
            print("(no preview or release alerts for today)")
            return 0
        for it in items:
            print(f"--- {it['title']} ({it['id']}) ---")
            print(it["body"])
            print()
        return 0

    if not args.report_id:
        print("report_id is required for preview/release mode", file=sys.stderr)
        return 2

    report = _find_report(plan, args.report_id)
    if not report:
        print(f"no report with id={args.report_id} in plan.json", file=sys.stderr)
        return 2

    if args.mode == "preview":
        print(format_preview_sms(report, plan))
    elif args.mode == "release":
        out = format_release_sms(report, plan)
        if not out:
            print("(below-threshold surprise — would not fire)")
        else:
            print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
