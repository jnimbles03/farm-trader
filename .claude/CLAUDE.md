# graphify
- **graphify** (`.claude/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

# Project context

Full architecture, API details, trade flow, Worker deployment notes, admin broadcast system,
known gotchas, and what is not yet built:
→ `PROJECT_CONTEXT.md` (repo root)

Read this at the start of every session before touching any code.

# Critical operational notes

- **Cloudflare Worker is NOT auto-deployed on push.** Any change to `cloudflare-worker/src/index.ts`
  requires the user to run `cd cloudflare-worker && npx wrangler deploy` locally to go live.
  Until deployed, admin routes, cron reminders, and auth fixes only exist in source.

- **AUTH_PHONES secret** gates both dashboard SMS login and trade OTP. The `ORDER_PHONE_WHITELIST`
  was removed from source; the worker now calls `parsePhoneList(env.AUTH_PHONES)` at runtime.

- **MakeOffer body is confirmed**: `{bidId, quantity: string, offerPrice: string, expiration: string}`.
  Validated from real filled offer #1727590147 (Jun 24 2026). Do not revert to speculative fields.

# What needs to happen next

1. User runs `wrangler deploy` to push the fixed ORDER_PHONE_WHITELIST + admin routes + cron live
2. Corn selling ladder: Jimmy provides levels + timing windows → store in `docs/corn_ladder.json`
3. Weekly Monday report: Monday 7am CDT cron, reads bids + ag calendar, TextBelt to group
   Recipients still needed: Kevin (Quigley?) and Allison — phone numbers required
