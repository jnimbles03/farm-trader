# SMS confirmation flow

When a signal fires, every recipient in `ALERT_PHONE` gets a text and
simply replies `Y` to confirm or `N` to veto. When everyone confirms,
the group gets a follow-up "all confirmed" SMS. Any veto immediately
notifies the group.

A 6-char `short_id` is still minted internally so the dashboard / state
file can distinguish simultaneous firings, but it's no longer printed
in the outbound SMS — keeping replies to a single keystroke. The
collector matches a bare `Y` / `N` against the *most recent* pending
prompt for the replying phone number.

```
evaluate.py (GHA cron)
    │  sends alert via TextBelt with replyWebhookUrl=cloudflare-worker
    │  writes state/confirmations.json with {sid, recipients: pending}
    ▼
TextBelt ──── delivers SMS ───▶ recipient phones
                                     │ reply "Y abc123"
                                     ▼
                           TextBelt webhook POST
                                     │
                                     ▼
                     Cloudflare Worker (this repo's cloudflare-worker/)
                                     │ verify HMAC → GitHub dispatch
                                     ▼
            .github/workflows/collect-reply.yml (repository_dispatch)
                                     │
                                     ▼
                     scripts/collect_reply.py
                     - updates state/confirmations.json
                     - on "all Y" → send group "confirmed" SMS
                     - on any "N" → send group "vetoed" SMS
                     - commits + pushes
```

## One-time setup (~20 min)

### 1. Deploy the Cloudflare Worker

See [`cloudflare-worker/README.md`](cloudflare-worker/README.md). At the
end you'll have a URL like
`https://freis-farm-sms-reply.<subdomain>.workers.dev`.

### 2. Add `REPLY_WEBHOOK_URL` to repo secrets

GitHub → Settings → Secrets and variables → Actions → New repository
secret:

- Name:  `REPLY_WEBHOOK_URL`
- Value: the worker URL from step 1

The `evaluate.yml` and `collect-reply.yml` workflows already reference
these secrets — no further YAML edits needed.

### 3. That's it

The next scheduled `evaluate.yml` run will include reply codes on any
signal-hit alert. Replies land in `state/confirmations.json` within
seconds of arriving at TextBelt (worker → GitHub dispatch → workflow
takes ~10-20s).

## File map

| File | Purpose |
|---|---|
| `cloudflare-worker/src/index.ts` | Verifies TextBelt HMAC, fires `repository_dispatch` |
| `cloudflare-worker/wrangler.toml` | Worker config + public env vars |
| `.github/workflows/collect-reply.yml` | Triggered by `repository_dispatch: sms_reply` |
| `.github/workflows/remind-pending.yml` | Per-minute cron that re-pings non-responders |
| `scripts/collect_reply.py` | Parses one reply, updates confirmations state, sends follow-up |
| `scripts/remind_pending.py` | Sweeps pending entries, sends reminders to recipients with `vote: null` |
| `state/confirmations.json` | Full state (includes phone numbers) — private |
| `docs/confirmations.json` | Sanitized state (vote tallies only) — public, for dashboard |
| `evaluate.py::_record_outbound` | Stamps outbound alerts into confirmations state |

## Reply parsing

The collector accepts:

- `Y` / `y` / `YES` / `yes` → confirm (matched to most recent pending for that phone)
- `N` / `n` / `NO` / `no` → veto (matched to most recent pending for that phone)
- `Y abc123` / `N abc123` → confirm/veto explicitly by short_id (still supported for the dashboard / manual testing)
- Anything else → stashed in `_orphans` for later review

The 6-char code is hex (`[a-f0-9]{6}`), generated as the first 6 chars of
`sha256(signal_key + iso_timestamp)` at send time. It's kept in
`state/confirmations.json` but not printed in outbound SMS.

## Reminders for non-responders

Recipients who ignore the original alert get re-pinged by
`scripts/remind_pending.py`, fired by `.github/workflows/remind-pending.yml`
on a per-minute cron in the same window as `evaluate.yml`.

Default cadence (all configurable via workflow env vars):

| Env var                | Default | Meaning                                        |
|------------------------|---------|------------------------------------------------|
| `FIRST_REMINDER_DELAY` | 300     | Seconds after `sent_at` before the 1st nudge   |
| `REMINDER_INTERVAL`    | 60      | Seconds between subsequent nudges              |
| `MAX_REMINDERS`        | 5       | Hard cap per recipient — after this, give up   |

So a recipient who never replies gets nudged at roughly t+5min, t+6min,
t+7min, t+8min, t+9min — then the sweep leaves them alone.

Only recipients whose `vote` is still `null` on a `status: pending`
entry get pinged. When they finally vote (or anyone votes `N`, flipping
the entry to `vetoed`), the sweep stops touching them.

Per-recipient bookkeeping is stamped into `state/confirmations.json`:

```json
"recipients": {
  "+13125551234": {
    "vote": null,
    "last_reminded_at": "2026-04-22T14:12:03+00:00",
    "reminders_sent": 3
  }
}
```

Important caveat: GitHub Actions cron drifts 0–15 min under load, so
the "every 1 minute" cadence is really a ceiling. When a backed-up
tick finally fires, the script catches up whatever was due. Don't
count on 1-minute SMS precision here — if you need sub-minute
guarantees, move the sweep to a real scheduler (e.g., Cloudflare Cron
Triggers) the same way the reply worker lives there today.

Cost note: every reminder to every non-responder burns 1 TextBelt
credit. With defaults (5 reminders × N recipients), a fully-ignored
alert consumes `5·N` credits before the cap kicks in.

## Disabling confirmations

To turn the feature off, delete the `REPLY_WEBHOOK_URL` repo secret. The
evaluator sees it as empty, skips the short_id, and sends alerts with no
reply instructions — the rest of the pipeline goes back to fire-and-forget.

To disable *just the reminders* while keeping the confirmation flow,
comment out the `cron:` line in `.github/workflows/remind-pending.yml`
and commit. `workflow_dispatch` still works for on-demand testing.

## Testing the whole flow

1. From the GitHub Actions tab, dispatch **Test SMS delivery**. You should
   get a plain test SMS (no confirmation tracking — `test-sms.yml`
   deliberately doesn't invoke the confirmation path).
2. Wait for the next scheduled `Evaluate grain signals` run (or dispatch it
   manually) with a signal in HIT state. You'll get an SMS ending with
   `Reply Y to confirm, N to veto.`
3. Reply `Y`. Within ~20s, a new commit should appear on `main`
   from `freis-farm-bot` updating `state/confirmations.json`.
4. When every recipient has replied Y, a final group SMS lands:
   `FREIS FARM OK All confirmed — <signal_key>`.

## Rotating the TextBelt key

If you rotate the TextBelt API key, update it in three places:

1. GitHub repo secret `TEXTBELT_KEY`
2. Worker secret: `cd cloudflare-worker && wrangler secret put TEXTBELT_KEY`
3. (nowhere else — `evaluate.py` and `collect_reply.py` both read from env)

The worker needs the key because it verifies the webhook HMAC using that
same key as the signing secret.
