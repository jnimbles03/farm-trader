# Coding-agent prompt — add an SMS bot route to the Freis Farm Worker

Copy everything below the `---` and paste it into your coding agent (Claude Code,
Cursor, etc.) as the task brief. It is self-contained — the agent does not need
prior context from any other conversation.

---

## Task

Extend the existing Cloudflare Worker at `farm-proxy/cloudflare-worker/src/index.ts`
to add a conversational SMS bot. When an inbound SMS arrives that is **not** a
short structured reply (the worker already handles `Y abc123` / `N abc123` votes
and order confirmations), forward the message to Claude and SMS the response
back to the sender.

The Worker today (read it first to ground yourself) is a stateless TextBelt
webhook receiver + OTP gateway that fires `repository_dispatch` events to
`jnimbles03/farm-trader`. Three routes:

- `POST /` — TextBelt inbound webhook → fires `event_type: sms_reply` to GitHub
- `POST /orders/start` — mints OTP, sends SMS, returns HMAC bundle
- `POST /orders/submit` — verifies HMAC + code, fires `event_type: order_draft`

You are adding a fourth behavior: after `textbeltReply` verifies the inbound HMAC
and parses `{ fromNumber, text }`, it should classify the message and either
keep the existing dispatch path (for structured votes/replies) **or** route to a
new `handleBotMessage()` function. Do not break the existing flow — the order
confirmation system in `farm-trader` depends on it.

## Decisions already made — do not ask the user

1. **LLM:** Anthropic Claude, model `claude-haiku-4-5-20251001`. Use the
   `https://api.anthropic.com/v1/messages` endpoint directly; do not pull in an
   SDK (Workers don't need it and it bloats the bundle).
2. **Memory:** Cloudflare KV namespace bound as `CONVOS`. Key = phone number,
   value = JSON array of `{role, content}` messages, keep last 20, 7-day TTL.
   Add the binding to `wrangler.toml` and document the
   `wrangler kv:namespace create CONVOS` command in the README.
3. **Secret:** new secret `ANTHROPIC_KEY`, set with `wrangler secret put`.
   Add to the `Env` interface and document in the file header comment.
4. **Reply length cap:** 300 characters / `max_tokens: 200`. Enforce in the
   system prompt AND truncate defensively in code before sending.
5. **System prompt:** the bot is "Jimmy's farm assistant for Freis Farm." It
   knows the farm grows corn, soy, and hay; sells through Ritchie Grain;
   markets old-crop and runs a 2026 Average Pricing Program (1,500 bu corn,
   500 bu soy). Keep replies plain text, no markdown — SMS doesn't render it.
   Replies must be ≤ 300 chars.
6. **Classifier (cheap, no LLM):** route to the existing GitHub dispatch path
   if the text matches `/^[YN]\s+[a-z0-9]{4,8}\b/i` (vote pattern) OR is a pure
   6-digit code (OTP). Everything else goes to the bot.
7. **Rate limit:** max 30 inbound bot messages per phone per UTC day, tracked
   in KV under key `rl:<phone>:<YYYY-MM-DD>` with a 25-hour TTL. On exceed,
   reply once with "Daily message limit reached. Resets at midnight UTC."
   then drop further messages silently.
8. **Phone allow-list:** for now reuse `ORDER_PHONE_WHITELIST`. Reject anything
   else with a 200 OK + no SMS sent (don't leak whether the number is known).
9. **Failure handling:** if the Anthropic call fails or times out (>15s),
   reply "Bot is having trouble. Try again in a minute." and log to console.
   Do not retry inline — TextBelt will not redeliver.

## Files to change

- `farm-proxy/cloudflare-worker/src/index.ts` — add `handleBotMessage`,
  `classifyInbound`, `callClaude`, `checkRateLimit` helpers; modify
  `textbeltReply` to branch.
- `farm-proxy/cloudflare-worker/wrangler.toml` — add `[[kv_namespaces]]`
  binding for `CONVOS`.
- `farm-proxy/cloudflare-worker/README.md` — add a "Bot mode" section with
  the new secret, KV setup command, and a one-paragraph description.

## Code structure

Mirror the existing style: top-of-file block comment listing routes/secrets,
small focused helpers, JSDoc-style comments on the *why* not the *what*,
constants at the top. The existing `sendTextBelt`, `hmacHex`, `timingSafeEqual`,
and `bytesToHex` helpers stay. Reuse `sendTextBelt` for the bot's outbound
SMS — do not duplicate that logic.

Keep the worker stateless except for the KV reads/writes. No Durable Objects,
no D1.

## Acceptance criteria

1. `wrangler deploy` succeeds with no TypeScript errors.
2. A `Y abc123` reply still hits the existing `repository_dispatch` path and
   fires `event_type: sms_reply`. Verify by adding a unit-style assertion or
   by reading the code path carefully — do not require the user to test live.
3. An arbitrary message like "what's corn doing today" routes to
   `handleBotMessage`, calls Claude, and triggers `sendTextBelt`.
4. Rate limit kicks in at message #31 of the day.
5. Non-whitelisted phones get 200 OK with no SMS sent.
6. Reply is ≤ 300 chars even if Claude over-produces.

## What to do when you're done

Print a diff summary, the exact `wrangler` commands the user needs to run
(`wrangler kv:namespace create CONVOS`, `wrangler secret put ANTHROPIC_KEY`,
`wrangler deploy`), and the one-line README addition. Do not assume the user
has already created the KV namespace or set the secret.

## What NOT to do

- Do not change the `/orders/start` or `/orders/submit` routes.
- Do not introduce a new HTTP framework, router library, or build tooling.
- Do not add npm dependencies — the worker should still be a single-file TS
  module deployable as-is.
- Do not log message contents to console (PII). Log only `phone` (last 4
  digits) and length / status.
- Do not add tests against the live Anthropic or TextBelt APIs.
