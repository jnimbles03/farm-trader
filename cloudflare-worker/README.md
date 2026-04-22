# freis-farm-sms-reply (Cloudflare Worker)

Single-purpose worker: takes TextBelt's reply webhook, verifies it's real via
HMAC, and forwards it to the farm-trader repo as a `repository_dispatch`
event so `collect-reply.yml` can record the vote in `state/confirmations.json`.

## One-time setup (~15 min)

1. **Sign up for Cloudflare** (free, no card) at https://dash.cloudflare.com/sign-up.
2. **Install wrangler**:
   ```bash
   npm install -g wrangler
   cd cloudflare-worker
   npm install
   wrangler login     # opens browser
   ```
3. **Create a GitHub fine-grained PAT** at
   https://github.com/settings/personal-access-tokens/new :
   - Resource owner: `jnimbles03`
   - Only selected repositories → `farm-trader`
   - Permissions → Repository permissions:
     - Contents: **Read and write**
     - Actions:  **Read and write**
     - Metadata: **Read-only** (auto-added)
   - Expiration: 1 year (set a calendar reminder to rotate)
   - Copy the token when shown — you won't see it again.
4. **Set worker secrets**:
   ```bash
   wrangler secret put TEXTBELT_KEY   # paste your TextBelt key
   wrangler secret put GITHUB_TOKEN   # paste the PAT from step 3
   ```
5. **Deploy**:
   ```bash
   wrangler deploy
   ```
   Wrangler prints a URL like
   `https://freis-farm-sms-reply.<your-subdomain>.workers.dev`. Copy it.
6. **Add `REPLY_WEBHOOK_URL` as a GitHub repo secret**
   (Settings → Secrets and variables → Actions → New repository secret)
   with the URL from step 5.

That's it. From now on every alert SMS will include a confirmation code
and recipients can reply `Y <code>` / `N <code>` to vote.

## Smoke test

After deploy, `curl` the worker's URL with GET — should return `200 OK`:

```bash
curl https://freis-farm-sms-reply.<your-subdomain>.workers.dev
```

Then tail logs while triggering the `test-sms` workflow from the GitHub UI:

```bash
wrangler tail
```

Reply `Y abc123` to the test SMS and you should see a `queued sms_reply`
line in the tail output, followed by a new commit on `main` from
`freis-farm-bot` updating `state/confirmations.json`.

## Updating

```bash
# edit src/index.ts
wrangler deploy
```

## Rotating the GitHub PAT

```bash
wrangler secret put GITHUB_TOKEN
# paste new token; old one keeps working until you revoke it on github.com
```
