# Farm-Trader Project Context

Freis family grain marketing dashboard. Lets one user (Mom, phone +16302479950)
draft and submit limit-sell orders against Akron/Ritchie elevator bids, watch
live prices, and receive SMS alerts when price targets are hit.

---

## Architecture at a glance

```
GitHub Pages (docs/)
  ├── index.html     — landing / overview / trade widget
  ├── home.html      — full grain ops dashboard / trade widget
  ├── hedge.html     — hedge analysis / trade widget
  ├── admin.html     — admin orchestration console (broadcast, groups, CTA)
  └── *.json         — data files updated by GitHub Actions crons

Cloudflare Worker  (cloudflare-worker/src/index.ts)
  freis-farm-sms-reply.freis.workers.dev
  ├── POST /orders/start         — mints OTP + HMAC-signed payload bundle
  ├── POST /orders/submit        — verifies OTP+HMAC → fires order_draft dispatch
  ├── POST /auth/login           — garage code → 30-day session token
  ├── POST /auth/sms-start       — mint + SMS OTP for dashboard login
  ├── POST /auth/sms-verify      — verify OTP → 30-day session token
  ├── POST /auth/verify          — re-check existing token (HMAC+expiry)
  ├── POST /advisor              — Claude advisor chat pass-through
  ├── POST /admin/sms-start      — admin OTP (24h token, ADMIN_PHONE only)
  ├── POST /admin/sms-verify     — verify admin OTP → admin token
  ├── POST /admin/state          — bootstrap: recipients + runs + groups + templates
  ├── POST /admin/recipients     — save recipients list to KV
  ├── POST /admin/groups         — save groups list to KV
  ├── POST /admin/templates      — save templates list to KV
  ├── POST /admin/alerts/config  — save alert thresholds to KV
  ├── POST /admin/broadcast/start|submit|test — CTA orchestration (OTP-gated live)
  ├── GET  /admin/runs           — recent broadcast run list
  └── GET  /admin/runs/<id>      — single run detail
  Cron trigger: every minute → scheduled() → processReminders()
  KV namespace: FARM_KV (id in wrangler.toml) — stores recipients, groups,
                templates, alert config, run snapshots

GitHub Actions  (.github/workflows/)
  accept-order.yml         — on order_draft dispatch → writes docs/orders.json
  promote-order.yml        — workflow_dispatch: draft→live + MakeOffer to Akron
  evaluate.yml             — every 15 min: prices, signals, SMS alerts
  refresh-bushel.yml       — every 15 min Mon–Fri 8am–4pm CDT: scrape Akron
  refresh-market-wire.yml  — 7am + 2pm CDT weekdays: yfinance news → news.json
  static.yml               — builds GitHub Pages

Python scripts
  evaluate.py              — price eval, SMS alerts via TextBelt
  scripts/scrape_bushel.py — Keycloak auth + Bushel API → docs/bushel.json
  scripts/promote_order.py — flip order draft→live by UUID
  scripts/submit_offer.py  — authenticate Bushel, call MakeOffer, write result
  scripts/accept_order.py  — called by accept-order workflow; writes orders.json
  scripts/refresh_market_wire.py — yfinance news → docs/news.json
  refresh_elevator_bids.py — FS Grain regional bid grid → docs/elevator_bids.json
```

---

## Data files in docs/ (served by GitHub Pages)

| File | Written by | Read by |
|------|-----------|---------|
| `bushel.json` | refresh-bushel.yml → scrape_bushel.py | dashboard JS, trade widget, submit_offer.py |
| `bushel_raw.json` | scrape_bushel.py | build_sales_log_from_bushel.py |
| `orders.json` | accept_order.py (via workflow), promote_order.py, submit_offer.py | trade widget (OPEN DRAFTS section) |
| `news.json` | refresh_market_wire.py | js-market-wire renderer in HTML |
| `elevator_bids.json` | refresh_elevator_bids.py | js-bid-updater in HTML |
| `prices.json` | evaluate.py | dashboard, refresh_market_wire.py |
| `signals.json` | evaluate.py | dashboard |
| `sales_log.json` | build_sales_log_from_bushel.py | season sales panel |

---

## Bushel / Akron API

**Auth**: Keycloak OIDC via NextAuth.js on portal.bushelpowered.com.
Full flow in `scripts/scrape_bushel.py` → `login()`.
- Portal: `https://portal.bushelpowered.com`
- ID host: `https://id.bushelops.com`
- Tenant slug (`app-company` header): `akronservices`
- Username: 10-digit phone number (NOT email)
- Credentials: GitHub secrets `BUSHEL_USER` / `BUSHEL_PASS` (also aliased as `AKRON_USER` / `AKRON_PASS`)

**Key API endpoints** (all POST, Bearer token + `app-company: akronservices`):
```
https://api.bushelpowered.com/api/markets/aggregator/bids/v1/GetBidsList
https://api.bushelpowered.com/api/markets/aggregator/offers/v1/ListOffers
https://api.bushelpowered.com/api/markets/aggregator/offers/v1/MakeOffer   ← SELL
https://api.bushelpowered.com/api/aggregator/grain/v1/GetAllContracts
https://api.bushelpowered.com/api/aggregator/accounts/v1/GetAllAccounts
https://centre.bushelops.com/api/v2/commodity-balances
https://centre.bushelops.com/api/v3/tickets
```

**MakeOffer request body** (confirmed from real filled offer `1727590147`, Jun 24 2026):
```json
{ "bidId": "<bid.id>", "quantity": "100", "offerPrice": "11.16", "expiration": "2026-06-30" }
```
- `quantity` and `offerPrice` are **strings**, not numbers
- Field names are `offerPrice` (not `targetPrice`) and `expiration` (not `expirationDate`)
- `ritchieBidLadder` in `bushel.json` carries each bid's `id` and `canMakeOffer: true/false`
- `canMakeOffer: true` means Akron is currently accepting offers on that delivery period

**Market open/closed signal**: any bid in `ritchieBidLadder` with `canMakeOffer: true`
AND `bushel.json.fetchedAt` < 75 minutes ago → market is open. Used by the trade widget badge.

---

## Trade order flow (end-to-end)

```
1. User opens trade drawer on dashboard
2. JS builds payload: {id, submission_id, crop, type, bushels, limit_price, expiry, ...}
   PENDING_PAYLOAD captured at send-code time — MUST reuse at submit (same UUID/HMAC)
3. POST /orders/start  → Worker mints OTP (TextBelt SMS) + HMAC bundle
   HMAC = hmacHex(TEXTBELT_KEY, `${code}|${phone}|${payloadHash}|${nonce}|${expiresAt}`)
   Phone whitelist: derived at runtime from AUTH_PHONES secret (NOT hardcoded)
4. User enters 6-digit code → POST /orders/submit
   Worker: re-derives HMAC with user code, verifies sha256(canonicalJson(payload)) == payload_hash
   → fires order_draft repository_dispatch
5. accept-order.yml runs: python scripts/accept_order.py → appends to docs/orders.json
   status: "draft"
6. To submit to Akron:
   Actions → promote-order.yml → Run workflow → paste order UUID
   → promote_order.py: draft→live
   → submit_offer.py: login Bushel → MakeOffer → status: "submitted" + bushel_offer_id
```

**OTP TTL**: 5 minutes. Trade phone whitelist: `parsePhoneList(env.AUTH_PHONES)` at runtime.

**Critical bug already fixed**: `buildPayload()` must NOT be called twice (generates new UUID+timestamp).
`PENDING_PAYLOAD` variable captures payload at send-code time and reuses at submit.

---

## Cloudflare Worker — deployment notes

**CRITICAL**: The source in `cloudflare-worker/` must be deployed manually. GitHub pushes do NOT
auto-deploy the worker. All admin routes, the cron trigger, and auth fixes only take effect after:

```bash
cd cloudflare-worker
npx wrangler deploy
```

**Required secrets** (set via `wrangler secret put <NAME>`):
- `TEXTBELT_KEY` — paid TextBelt key; also used as HMAC secret for all tokens
- `GITHUB_TOKEN` — fine-grained PAT with Contents+Actions r/w on farm-trader
- `GARAGE_CODE` — legacy 4-digit dashboard PIN
- `AUTH_PHONES` — comma-separated E.164 phones allowed to log in AND submit trade orders
- `ANTHROPIC_API_KEY` — Claude API key for /advisor route

**KV namespace**: `FARM_KV` (binding in wrangler.toml, id `89ac203079404b90b032798eea908a05`)
Stores: recipients, groups, templates, alert config, run snapshots.

**Admin token**: distinct HMAC namespace from dashboard auth (`admin|ADMIN_PHONE|expiresAt` vs
`auth|expiresAt`). 24-hour TTL. ADMIN_PHONE is hardcoded to `+16302479950` in source.

**Cron trigger** (`* * * * *`): fires `scheduled()` → `processReminders()` every minute.
Walks pending CTA runs and sends reminder SMS to non-respondents. ONLY active when worker is deployed.

---

## Admin broadcast system (docs/admin.html)

Separate console at `/admin.html`. Login via admin OTP (SMS to ADMIN_PHONE).

**Concepts**:
- **Recipients**: named list stored in KV (admin:recipients:v1). Each has name, phone, required flag.
  Default required quorum names: Dan Cooke, Susan Lindeen, Maryann Meyer.
- **Groups**: subsets of recipients for targeted broadcasts (admin:groups:v1).
- **Templates**: reusable message bodies with `{bushels}`, `{price}`, `{crop}` tokens (admin:templates:v1).
- **Bulletin**: one-way broadcast SMS. No Y/N quorum needed.
- **CTA** (Call to Action): requires Y/N reply quorum from `required` recipients before marking complete.
  Live CTAs need a fresh OTP code (prevents accidental sends).
- **Test**: sends only to ADMIN_PHONE; never touches recipients list.

**Run lifecycle**: `pending` → `quorum_met` / `rejected` / `complete` / `failed`
Reminder cron fires every minute, sends reminders at 5-min intervals, max 8 cycles.

---

## GitHub Actions secrets

| Secret | Used by |
|--------|---------|
| `TEXTBELT_KEY` | evaluate.py, Worker (SMS OTP + alerts + HMAC) |
| `ALERT_PHONE` | evaluate.py (price alert target) |
| `TRADE_PHONES` | evaluate.py |
| `NEWS_PHONES` | evaluate.py |
| `BUSHEL_USER` | refresh-bushel.yml, promote-order.yml (Akron phone/username) |
| `BUSHEL_PASS` | refresh-bushel.yml, promote-order.yml |
| `REPLY_WEBHOOK_URL` | evaluate.py |
| `GH_PAT` | Cloudflare Worker (repository_dispatch auth) |

---

## orders.json schema

```json
{
  "orders": [
    {
      "id": "<uuid>",
      "submission_id": "<uuid>",
      "crop": "soy",
      "type": "limit",
      "bushels": 500,
      "limit_price": 11.50,
      "expiry": "2026-08-01",
      "status": "draft|live|submitted|submit_error",
      "created_at": "...",
      "accepted_at": "...",
      "phone": "+16302479950",
      "nonce": "...",
      "promoted_at": "...",       // set when draft→live
      "submitted_at": "...",      // set when submitted to Akron
      "bushel_offer_id": "...",   // returned by MakeOffer
      "submit_error": "..."       // set on submit_error status
    }
  ]
}
```

**Status lifecycle**: `draft` → `live` → `submitted` (or `submit_error`)
`draft` = inert. `live` = picked up by submit_offer.py. `submitted` = standing offer at Akron.

---

## Front-end JS patterns

**js-bid-updater** (inline `<script id="js-bid-updater">` in each HTML file):
- Fetches `elevator_bids.json`, `bushel.json`, `ritchie_live.json` (from advisor/)
- Maps to keys like `ritchie.corn_bid`, `ritchie.soy_bid`, `ritchie.corn_basis_cents`
- Fills any `<span class="js-bid" data-key="..." data-fmt="price|basis|bu|money">` element
- `BIDS` and `AVAIL` globals for the trade widget

**js-market-wire renderer**: reads `news.json`, handles both schemas:
- New (refresh_market_wire.py): `{id, tag, tag_class, text, published_at}`
- Old (manual): `{id, date, title, impact, affects, source}` — `tagFrom()` helper infers tag

**Market status badge** (`#trade-market-status`):
- Green "Open" / grey "Closed" pill in the trade widget avail strip
- Logic: `canMakeOffer` in `ritchieBidLadder` AND `bushel.json` age < 75 min

**Trade price auto-refresh**: `refreshTradePrices()` runs on load then every 5 min via `setInterval`.
Re-fetches `bushel.json` with `cache: 'no-store'`, updates `BIDS`, `AVAIL`, bid display, and market badge.
**TRANCHES guard**: if the trade drawer is open (`#trade-drawer.classList.contains('open')`),
the refresh skips the `TRANCHES = [defaultTranche()]` reset — preserves in-progress order edits.

**TRADE_API_BASE**: set to `"https://freis-farm-sms-reply.freis.workers.dev"` in all three HTML files.
Empty string = stub mode (codes shown on-screen only, no real SMS).

**Default tranche**: `limit_price: round2(BIDS[tradeCrop])` — current Ritchie bid.

---

## deps / requirements

`requirements.txt` at root only has `httpx` + `yfinance` (used by evaluate.py and refresh scripts).
Bushel scripts install separately: `pip install requests beautifulsoup4 lxml python-dotenv`
(see promote-order.yml and refresh-bushel.yml install steps).

---

## Known gotchas

- **Worker must be manually deployed**: `cd cloudflare-worker && npx wrangler deploy`. GitHub pushes
  do NOT redeploy the worker. Admin routes, cron reminders, and auth fixes only land after a deploy.
- **Keycloak split-flow**: POST 1 sends username, POST 2 sends password. Each returns new session_code
  in the form action URL. `_find_form_action()` in scrape_bushel.py handles both HTML form tags
  and JS-rendered pages.
- **Crop name mapping**: order uses `"soy"`, Bushel API / ritchieBidLadder uses `"soybeans"`.
  `CROP_KEY` dict in submit_offer.py handles this.
- **bushel.json fetchedAt**: set by `scrape_bushel.py`. Outside Mon–Fri 8am–4pm CDT, data goes
  stale and market badge flips to Closed automatically (75-min threshold).
- **Git push races**: evaluate.yml, refresh-bushel.yml, and other crons push to main concurrently.
  Occasional non-fast-forward failures are expected; the workflows retry.

---

## What is NOT yet built

- **Weekly Monday report**: Monday 7am CDT cron → Python script reads bids + ag calendar → composes
  concise SMS → TextBelt to group (Maryann, Susan, Dan, Kevin, Allison, Jimmy). Corn selling ladder
  config file not yet created (pending levels from Jimmy).
- **Corn selling ladder**: target levels + timing windows not yet stored in repo. Needed to drive
  the weekly report and give context to each trade.
- evaluate.py does not yet monitor `live` or `submitted` orders (no fill detection)
- No webhook or polling to detect when Akron fills an offer
- No "cancel offer" flow (would need a DeleteOffer/CancelOffer endpoint, not yet explored)
