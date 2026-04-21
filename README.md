# Freis Farm — grain signals on GitHub Actions

Zero-server pipeline for the Freis Farm dashboard. Every 15 minutes during the CBOT grain day session, a GitHub Action:

1. Pulls current grain futures from Yahoo Finance (~10 min delayed).
2. Evaluates your open Bushel contracts against a signal model.
3. Writes `docs/prices.json` + `docs/signals.json` back to the repo.
4. Fires a TextBelt SMS if any signal flipped from WAIT to HIT.
5. Commits the updated files.

GitHub Pages serves `docs/` as a static site. The React dashboard at `farm.meyerinterests.com` fetches `prices.json` from there instead of calling a live server.

## Why this way

- **No server to pay for or watch.** The Action runs on GitHub's dime.
- **Deploys are `git push`.** No CLI, no Docker, no SSH.
- **State is in the repo.** `alerts_state.json` is committed between runs, so cooldowns survive restarts.
- **Price freshness is 15 minutes.** The React site already has 30-second in-page timers; the upstream limit is the cron cadence.

If you later need sub-minute prices or an on-demand HTTP endpoint, graduate to a real server — the archived Fly config in `_archive/fly/` still works.

## Files

```
farm-proxy/
├── evaluate.py                    one-shot script the Action runs
├── contracts.json                 your open Bushel contracts — edit via GitHub UI
├── requirements.txt               yfinance + httpx, nothing else
├── .github/workflows/evaluate.yml cron + commit-back workflow
├── docs/
│   ├── index.html                 human-readable landing page
│   ├── prices.json                latest prices (written by the Action)
│   ├── signals.json               latest signal evaluation
│   └── last_run.json              run metadata
├── state/
│   └── alerts_state.json          per-signal cooldown state
├── jsx-patch.md                   how to point the React site at the new URL
└── _archive/fly/                  old Fly.io config — ignore
```

## Setup — first-time only

### 1. Create a new GitHub repo and push

From `farm-proxy/`:

```bash
cd "farm-proxy"
git init
git add .
git commit -m "initial commit — grain signal pipeline"

# Create the repo on GitHub first (web UI is fine), then:
git branch -M main
git remote add origin git@github.com:YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Repo can be public or private. Public is simpler — Pages is instant and Actions minutes are unlimited.

### 2. Add secrets for SMS

In the repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add two:

| Name           | Value |
|----------------|-------|
| `TEXTBELT_KEY` | Your TextBelt paid key (get one at textbelt.com — ~$10 for 250 SMS) |
| `ALERT_PHONE`  | E.164-ish, e.g. `+13125551234` |

Without these, the pipeline still runs and updates prices; it just won't text you.

### 3. Enable GitHub Pages

In the repo: **Settings → Pages**.

- **Source:** Deploy from a branch
- **Branch:** `main`
- **Folder:** `/docs`
- Click **Save**.

Within a minute Pages publishes at `https://YOUR_USER.github.io/YOUR_REPO/`. Visit it to confirm the landing page loads. (It will show "failed to load" until the first Action run writes the JSON files — that's fine.)

### 4. Kick off the first Action run

In the repo: **Actions → Evaluate grain signals → Run workflow → main → Run workflow**.

Watch it run. It should:
- Check out the repo
- Install yfinance + httpx
- Run `evaluate.py`
- Commit new `docs/` + `state/` files back to main

After it finishes, refresh `https://YOUR_USER.github.io/YOUR_REPO/` — the board should be live.

### 5. Patch the React site

See `jsx-patch.md`. Replace the broken `fetchGrainPrices()` in `freis-farm-v5.jsx` with a fetch against `https://YOUR_USER.github.io/YOUR_REPO/prices.json`, redeploy the site.

## Day-to-day use

### Add a new contract

Edit `contracts.json` on GitHub (pencil icon, commit straight to main). The next Action run picks it up and starts evaluating signals against it.

### Change signal logic

Edit the `model()` function in `evaluate.py` and push. The next run uses the new logic.

### Check if alerts are wired

Look at `docs/last_run.json` — `sms_ready` tells you whether the secrets are configured. `sms_fired` is incremented only on SMS TextBelt confirms as sent (not attempted). If you want a no-risk end-to-end test, force a hit by temporarily lowering a target in `model()`, push, trigger the workflow manually, confirm the text lands, then revert.

### Pause alerts

Simplest: comment out the `cron:` trigger in `.github/workflows/evaluate.yml` and commit. `workflow_dispatch` still works for on-demand runs.

## Local dev

```bash
cd farm-proxy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run once, dry (no SMS because env vars aren't set)
python evaluate.py

# With SMS — will actually text you
export TEXTBELT_KEY=textbelt_test        # free once-per-day-per-IP key
export ALERT_PHONE="+13125551234"
python evaluate.py
```

Inspect the written files under `docs/` and `state/` to confirm shape.

## Known limits

- GitHub Actions cron can be delayed 0–15 min under load. Acceptable here.
- Scheduled workflows only run on the default branch.
- If the repo has zero activity for ~60 days, GitHub disables scheduled workflows until you re-enable them. Not an issue if you're editing `contracts.json` occasionally.
- No exchange-holiday calendar — the cron runs Mon–Fri regardless. On a holiday Yahoo returns the prior close, signals don't move, no SMS fires. Fine.
- `alerts_state.json` is committed to history, so cooldown resets are auditable in `git log`.
