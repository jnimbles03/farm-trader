# SMS confirmation flow

When a signal fires, every recipient in `ALERT_PHONE` gets a text with a
6-char confirmation code. They reply `Y abc123` to confirm or `N abc123`
to veto. When everyone confirms, the group gets a follow-up "all confirmed"
SMS. Any veto immediately notifies the group.

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
| `scripts/collect_reply.py` | Parses one reply, updates confirmations state, sends follow-up |
| `state/confirmations.json` | Full state (includes phone numbers) — private |
| `docs/confirmations.json` | Sanitized state (vote tallies only) — public, for dashboard |
| `evaluate.py::_record_outbound` | Stamps outbound alerts into confirmations state |

## Reply parsing

The collector accepts:

- `Y abc123` / `y abc123` / `YES abc123` → confirm
- `N abc123` / `n abc123` / `NO abc123` → veto
- `Y` / `N` alone → inferred to the most recent pending prompt for that phone
- Anything else → stashed in `_orphans` for later review

The 6-char code is hex (`[a-f0-9]{6}`), generated as the first 6 chars of
`sha256(signal_key + iso_timestamp)` at send time.

## Disabling confirmations

To turn the feature off, delete the `REPLY_WEBHOOK_URL` repo secret. The
evaluator sees it as empty, skips the short_id, and sends alerts with no
reply instructions — the rest of the pipeline goes back to fire-and-forget.

## Testing the whole flow

1. From the GitHub Actions tab, dispatch **Test SMS delivery**. You should
   get a plain test SMS (no reply code — `test-sms.yml` deliberately
   doesn't invoke the confirmation path).
2. Wait for the next scheduled `Evaluate grain signals` run (or dispatch it
   manually) with a signal in HIT state. You'll get an SMS ending with
   `Reply 'Y abc123' to confirm, 'N abc123' to veto.`
3. Reply `Y abc123`. Within ~20s, a new commit should appear on `main`
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
