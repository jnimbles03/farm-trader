#!/usr/bin/env bash
#
# refresh_ib_iv.sh — wrapper for the launchd cron.
#
# Runs refresh_ib_iv.py against the local IB Gateway, then commits + pushes
# the updated JSON sidecar so GitHub Pages picks it up. Designed to be
# invoked by com.freis.refresh_ib_iv.plist at 16:30 CT M-F on the always-on
# Mac mini.
#
# Exit codes:
#   0 — refreshed and pushed (or no diff, nothing to push)
#   1 — Python script failed
#   2 — git operation failed
#
set -euo pipefail

REPO="${REPO:-$HOME/Documents/Claude/Projects/Freis Farm}"
PYTHON="${PYTHON:-/usr/bin/env python3}"

cd "$REPO"

# Pull first so we don't fight other commits (e.g., GHA bushel refresh).
git pull --rebase --autostash --quiet || true

# Run the IV pull. Fail loudly if it bombs.
$PYTHON farm-proxy/refresh_ib_iv.py || exit 1

# Stage the JSON sidecar only — leave anything else the user is editing alone.
git add farm-proxy/docs/advisor/ib_iv.json

# Bail quietly if nothing changed (market closed, IB returned identical IV).
if git diff --cached --quiet; then
  echo "[refresh_ib_iv.sh] no diff — nothing to commit"
  exit 0
fi

git commit -m "data: refresh ib_iv $(date '+%Y-%m-%d %H:%M %Z')" || exit 2
git push --quiet || exit 2

echo "[refresh_ib_iv.sh] pushed updated ib_iv.json"
