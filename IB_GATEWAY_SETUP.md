# IB Gateway → AgDCA live IV

End-of-day IV pull from Interactive Brokers, written to
`farm-proxy/docs/advisor/ib_iv.json`, consumed by the AgDCA tab on
`hedge.html`. Runs M–F at 16:30 CT on the always-on Mac mini that hosts IB
Gateway. Pushes the JSON to GitHub so Pages picks it up.

## One-time setup (Mac mini)

### 1. IB Gateway
Install the standalone IB Gateway (lighter than TWS):
<https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>

Log in with the account that has CME futures-options market data subscribed.
Even with EOD-only pulls, you need a market-data subscription for CBOT
options (the "CBOT Real-Time" + "CME-NYMEX-Globex Real-Time" bundles cover
ZSN6 options). EOD historicals from delayed feeds will not return
`modelGreeks.impliedVol`.

In IB Gateway → Configure → Settings → API → Settings:
- Enable ActiveX and Socket Clients ✓
- Socket port: **4001** (live) — or 4002 if running paper
- Bypass Order Precautions for API Orders: leave default
- Trusted IPs: add `127.0.0.1`
- Read-Only API: ✓ (we never place orders from this script)

Restart IB Gateway after changing API settings.

### 2. IBC (auto-restart, handle the daily 2FA)
IB Gateway logs you out at midnight Eastern unless you use IBC. Install:

```
brew install --cask ibc          # or download from github.com/IbcAlpha/IBC
```

Configure IBC with the gateway path and your username; enable auto-restart.
Without IBC, the cron will start failing every morning until you re-login.

### 3. Python env
On the Mac mini:

```
cd ~/Documents/Claude/Projects/Freis\ Farm
python3 -m pip install ib_insync
```

Smoke test:

```
python3 farm-proxy/refresh_ib_iv.py
```

Should print something like:
```
[refresh_ib_iv] connecting → 127.0.0.1:4001
[refresh_ib_iv] qualified ZSN6 (conId=...)
[refresh_ib_iv] futures last = 1203.0
[refresh_ib_iv] chain exp=20260522 · atm_strike=1200.0
[refresh_ib_iv] wrote .../farm-proxy/docs/advisor/ib_iv.json
[refresh_ib_iv] ATM IV = 15.42% (call+put avg) · calm · headroom 51.8%
```

If you see "No implied vol returned" — your CME options market-data
subscription isn't active. Check IBKR account → Settings → Market Data
Subscriptions.

### 4. Git push from cron
The wrapper (`refresh_ib_iv.sh`) does `git pull --rebase`, `git add`,
`git commit`, `git push`. The Mac mini needs:
- An SSH key registered with the GitHub repo (Settings → Deploy keys, write
  access, or your personal SSH key)
- `git remote -v` pointing at SSH (`git@github.com:...`), not HTTPS

Manual smoke:
```
bash farm-proxy/refresh_ib_iv.sh
```

### 5. Install the launchd job
```
cp farm-proxy/com.freis.refresh_ib_iv.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.freis.refresh_ib_iv.plist
```

Trigger immediately to verify the cron path works:
```
launchctl start com.freis.refresh_ib_iv
tail -f /tmp/refresh_ib_iv.out.log
```

If you change the plist later, `launchctl unload && launchctl load`.

## Tuning

- **Different position?** Edit the `POSITION` dict at the top of
  `refresh_ib_iv.py` AND the matching `POSITION` const in `hedge.html`.
  Keep them in sync — the script needs to know what to pull, the page
  needs to know what to display against.
- **Stop level?** Currently 32 (matches the static text on the trade
  ticket). Move it in `POSITION["stop_iv_pct"]` and the JSON will
  re-derive headroom + status.
- **Different cadence?** Add more `StartCalendarInterval` entries to the
  plist. Intraday will need a real market-data subscription, not just EOD.

## What the JSON looks like

```json
{
  "version": "0.1",
  "updated": "2026-05-04T21:30:14+00:00",
  "as_of_local": "2026-05-04 16:30 CDT",
  "position": "SN26 covered call",
  "instrument": {
    "underlying": "ZS",
    "local_symbol": "ZSN6",
    "fut_month": "202607",
    "future_last": 1203.0
  },
  "atm": {
    "chain_expiry": "20260522",
    "strike": 1200.0,
    "call_iv": 0.1543,
    "put_iv":  0.1559,
    "atm_iv":  0.1551,
    "iv_pct":  15.51,
    "iv_basis": "call+put avg"
  },
  "stop": {
    "stop_pct":  32.0,
    "headroom_pct": 51.5,
    "proximity_ratio": 0.485,
    "status": "calm"
  },
  "source": "IBKR · IB Gateway · 127.0.0.1:4001"
}
```

`status` cycles `calm` → `warming` (IV at 60% of stop) → `stop_zone`
(IV at 85% of stop). The hedge page changes the dot color and the
description copy based on this field.
