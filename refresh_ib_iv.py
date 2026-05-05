"""
refresh_ib_iv.py — pulls the at-the-money implied vol for the actively-modeled
grain options trade from a locally-running Interactive Brokers Gateway / TWS,
writes farm-proxy/docs/advisor/ib_iv.json. Consumed by hedge.html's AgDCA tab
to drive the "Vol vs stop" guard-rail value with a real number instead of the
hand-coded 15.5.

Designed to run end-of-day (16:30 CT after the grain pit close) on the
always-on Mac mini that hosts IB Gateway. Cron via launchd; commit + push
handled by the wrapper script `refresh_ib_iv.sh`.

Connection:
  IB Gateway 4001 (live), 4002 (paper), or TWS 7496/7497.
  Configure via IB_HOST, IB_PORT, IB_CLIENT_ID env vars; defaults to local 4001.

Position context:
  Hardcoded to the SN26 covered call. To trade-out and re-target, change the
  POSITION dict here AND the matching POSITION const in hedge.html so the
  script and the page agree on what "the trade" is.

Required:
  pip install ib_insync

Pipeline:
  1. Connect to local IB Gateway.
  2. Qualify the futures contract (ZSN6 = Soybean Jul '26).
  3. Pull the futures snapshot price.
  4. Request the futures-option chain via reqSecDefOptParams.
  5. Pick the chain expiry closest to the trade's option expiry.
  6. Pick the ATM strike (closest to the futures last).
  7. Request market data with genericTickList=106 to get modelGreeks.
  8. Average call + put IV; write the JSON sidecar.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

try:
    from ib_insync import IB, Future, FuturesOption  # type: ignore
except ImportError:
    sys.stderr.write(
        "ib_insync not installed. Run: pip install ib_insync\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Position context — keep in sync with the POSITION const in hedge.html.
# ---------------------------------------------------------------------------
POSITION = {
    "name":              "SN26 covered call",
    "underlying":        "ZS",          # Soybean futures (CBOT)
    "fut_month":         "202607",      # Jul '26 → ZSN6
    "exchange":          "CBOT",
    "currency":          "USD",
    "option_target_exp": "20260522",    # 3-week serial we sold against
    "stop_iv_pct":       32.0,          # cover if ATM IV pushes past this
}

IB_HOST      = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT      = int(os.environ.get("IB_PORT", "4001"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "11"))

HERE = Path(__file__).resolve().parent


def _find_docs_dir() -> Path:
    """Locate farm-proxy/docs regardless of where this script sits."""
    for candidate in (
        HERE / "farm-proxy" / "docs",
        HERE / "docs",
        HERE.parent / "docs",
        HERE.parent / "farm-proxy" / "docs",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    raise SystemExit("Could not locate farm-proxy/docs directory")


def _snapshot_price(ib: "IB", contract) -> Optional[float]:
    """reqMktData snapshot — returns last/close/marketPrice when settled."""
    ticker = ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
    # Snapshot mode is unreliable for some CME futures; use streaming + sleep.
    ib.sleep(2.5)
    px = (
        getattr(ticker, "last", None)
        or getattr(ticker, "close", None)
        or ticker.marketPrice()
    )
    ib.cancelMktData(contract)
    return float(px) if px and not (isinstance(px, float) and px != px) else None  # NaN guard


def _pick_chain(chains, fut) -> Optional[object]:
    """Match the chain whose tradingClass aligns with the futures contract."""
    if not chains:
        return None
    same_tc = [c for c in chains if c.tradingClass == fut.tradingClass]
    if same_tc:
        return same_tc[0]
    return chains[0]


def _pick_chain_expiry(expirations, target: str) -> str:
    """Pick the chain expiry closest to the trade's option expiry target."""
    if not expirations:
        raise RuntimeError("No expirations in option chain")
    expirations = sorted(expirations)
    target_dt = dt.datetime.strptime(target, "%Y%m%d")
    return min(
        expirations,
        key=lambda e: abs(dt.datetime.strptime(e, "%Y%m%d") - target_dt),
    )


def _pick_atm_strike(strikes, future_last: float) -> float:
    if not strikes:
        raise RuntimeError("No strikes in option chain")
    return min(strikes, key=lambda s: abs(s - future_last))


def main() -> int:
    ib = IB()
    print(f"[refresh_ib_iv] connecting → {IB_HOST}:{IB_PORT}")
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=10)

    try:
        fut = Future(
            symbol=POSITION["underlying"],
            lastTradeDateOrContractMonth=POSITION["fut_month"],
            exchange=POSITION["exchange"],
            currency=POSITION["currency"],
        )
        ib.qualifyContracts(fut)
        if not fut.conId:
            raise RuntimeError(f"Could not qualify futures contract for {POSITION['underlying']} {POSITION['fut_month']}")

        print(f"[refresh_ib_iv] qualified {fut.localSymbol} (conId={fut.conId})")

        future_last = _snapshot_price(ib, fut)
        if not future_last:
            raise RuntimeError("Could not fetch futures last price")
        print(f"[refresh_ib_iv] futures last = {future_last}")

        chains = ib.reqSecDefOptParams(fut.symbol, fut.exchange, "FUT", fut.conId)
        chain = _pick_chain(chains, fut)
        if not chain:
            raise RuntimeError("No option chain returned")

        target_exp = _pick_chain_expiry(chain.expirations, POSITION["option_target_exp"])
        atm_strike = _pick_atm_strike(chain.strikes, future_last)
        print(f"[refresh_ib_iv] chain exp={target_exp} · atm_strike={atm_strike}")

        call = FuturesOption(
            symbol=fut.symbol,
            lastTradeDateOrContractMonth=target_exp,
            strike=atm_strike, right="C",
            exchange=fut.exchange, currency=POSITION["currency"],
            tradingClass=chain.tradingClass,
        )
        put = FuturesOption(
            symbol=fut.symbol,
            lastTradeDateOrContractMonth=target_exp,
            strike=atm_strike, right="P",
            exchange=fut.exchange, currency=POSITION["currency"],
            tradingClass=chain.tradingClass,
        )
        ib.qualifyContracts(call, put)

        # genericTickList "106" = Option Implied Volatility (computed by IB)
        call_t = ib.reqMktData(call, "106", snapshot=False, regulatorySnapshot=False)
        put_t  = ib.reqMktData(put,  "106", snapshot=False, regulatorySnapshot=False)
        ib.sleep(3.0)

        def _iv(t):
            mg = getattr(t, "modelGreeks", None)
            if mg and mg.impliedVol and mg.impliedVol == mg.impliedVol:  # NaN guard
                return float(mg.impliedVol)
            return None

        call_iv = _iv(call_t)
        put_iv  = _iv(put_t)
        ib.cancelMktData(call)
        ib.cancelMktData(put)

        if call_iv and put_iv:
            atm_iv = (call_iv + put_iv) / 2.0
            iv_basis = "call+put avg"
        elif call_iv:
            atm_iv = call_iv
            iv_basis = "call only"
        elif put_iv:
            atm_iv = put_iv
            iv_basis = "put only"
        else:
            raise RuntimeError("No implied vol returned for ATM call or put — check IB market-data subscription for CME options")

        iv_pct      = round(atm_iv * 100.0, 2)
        stop_pct    = float(POSITION["stop_iv_pct"])
        headroom    = max(0.0, 1.0 - iv_pct / stop_pct)
        proximity   = iv_pct / stop_pct  # 0 = far from stop, 1 = at stop
        if   proximity < 0.60:  status = "calm"
        elif proximity < 0.85:  status = "warming"
        else:                   status = "stop_zone"

        out = {
            "version":  "0.1",
            "updated":  dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "as_of_local": dt.datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip(),
            "position": POSITION["name"],
            "instrument": {
                "underlying":   fut.symbol,
                "local_symbol": fut.localSymbol,
                "fut_month":    POSITION["fut_month"],
                "future_last":  future_last,
            },
            "atm": {
                "chain_expiry": target_exp,
                "strike":       atm_strike,
                "call_iv":      call_iv,
                "put_iv":       put_iv,
                "atm_iv":       atm_iv,
                "iv_pct":       iv_pct,
                "iv_basis":     iv_basis,
            },
            "stop": {
                "stop_pct":         stop_pct,
                "headroom_pct":     round(headroom * 100.0, 1),
                "proximity_ratio":  round(proximity, 3),
                "status":           status,  # calm | warming | stop_zone
            },
            "source": f"IBKR · IB Gateway · {IB_HOST}:{IB_PORT}",
        }

        target = _find_docs_dir() / "advisor" / "ib_iv.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(out, indent=2) + "\n")
        print(f"[refresh_ib_iv] wrote {target}")
        print(f"[refresh_ib_iv] ATM IV = {iv_pct}% ({iv_basis}) · {status} · headroom {out['stop']['headroom_pct']}%")
        return 0

    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
