// Freis Farm Worker — TextBelt reply webhook + draft-order OTP submit + auth gate.
//
// Routes:
//   GET  /                   health check
//   POST /                   TextBelt inbound webhook → fires sms_reply dispatch
//   POST /orders/start       mints 6-digit code, SMS to whitelisted phone,
//                            returns HMAC-signed nonce/expiry. Stateless.
//   POST /orders/submit      verifies HMAC + code, fires order_draft dispatch
//   POST /auth/login         takes {code}, checks against GARAGE_CODE secret,
//                            returns HMAC-signed token good for 30 days
//   POST /auth/verify        takes {token, expires_at}, returns {ok}
//   POST /auth/sms-start     takes {phone}, validates against AUTH_PHONES,
//                            mints 6-digit OTP, sends via TextBelt, returns
//                            HMAC-signed {nonce, expires_at, hmac}.
//   POST /auth/sms-verify    takes {phone, code, nonce, expires_at, hmac},
//                            verifies, returns 30-day session token (same
//                            shape as /auth/login).
//
// HMAC binds {code | phone | payload_hash | nonce | expires_at} so the server
// can verify a returning code without remembering anything between requests.
// TEXTBELT_KEY is reused as the HMAC secret — same trust boundary, no new
// secrets to wrangler-set.
//
// Required secrets (set via `wrangler secret put`):
//   TEXTBELT_KEY  — paid TextBelt key; doubles as HMAC secret
//   GITHUB_TOKEN  — fine-grained PAT, Contents + Actions r/w on farm-trader
//   GARAGE_CODE   — 4-digit garage code that gates the dashboard (legacy)
//   AUTH_PHONES   — comma-separated E.164 phones allowed to log in via SMS,
//                   e.g. "+16302479950,+16305551234"
//
// Required vars (set in wrangler.toml under [vars]):
//   GITHUB_REPO   — "owner/name", e.g. "jnimbles03/farm-trader"

export interface Env {
  TEXTBELT_KEY:      string;
  GITHUB_TOKEN:      string;
  GITHUB_REPO:       string;
  GARAGE_CODE:       string;
  AUTH_PHONES:       string;
  ANTHROPIC_API_KEY: string;
  // URL of the public advisor_context.json on GitHub Pages, e.g.
  //   https://jnimbles03.github.io/farm-trader/advisor/advisor_context.json
  // Falls back to a hardcoded path if unset.
  ADVISOR_CONTEXT_URL: string;
}

// Auth token TTL — how long a logged-in browser stays unlocked.
const AUTH_TTL_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

// SMS OTP TTL — same as the trade widget. Long enough to read + type 6 digits,
// short enough that intercepted codes age out before they're useful.
const AUTH_OTP_TTL_MS = 5 * 60 * 1000;

interface TextBeltReply {
  textId?: string;
  fromNumber?: string;
  text?: string;
}

// Phone whitelist for order submission. The dashboard sends to this exact
// number; anything else is rejected so a stray browser can't spam SMS.
const ORDER_PHONE_WHITELIST = ["+16302479950"];

// OTP expiry — short enough that intercepted codes age out fast, long enough
// that the user has time to read the SMS and type 6 digits.
const OTP_TTL_MS = 5 * 60 * 1000;

// CORS — the dashboard is on a different origin (github.io) from this worker.
// The wildcard is fine for our threat model: the only thing this server does
// for browser callers is fire SMS to a hardcoded phone and dispatch order
// drafts that still need an OTP code that only that phone can see.
const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
      ...(init.headers ?? {}),
    },
  });
}

function textResponse(body: string, init: ResponseInit = {}): Response {
  return new Response(body, {
    ...init,
    headers: { ...CORS_HEADERS, ...(init.headers ?? {}) },
  });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    // CORS preflight
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    // Health check
    if (req.method === "GET") {
      return textResponse("freis-farm-worker OK\n", { status: 200 });
    }

    if (req.method !== "POST") {
      return textResponse("Method not allowed", { status: 405 });
    }

    // Route by path. Anything unrecognized → fall through to the legacy
    // TextBelt handler (which is what / serves) so existing webhook URLs
    // configured at TextBelt keep working.
    if (url.pathname === "/orders/start") {
      return ordersStart(req, env);
    }
    if (url.pathname === "/orders/submit") {
      return ordersSubmit(req, env);
    }
    if (url.pathname === "/auth/login") {
      return authLogin(req, env);
    }
    if (url.pathname === "/auth/verify") {
      return authVerify(req, env);
    }
    if (url.pathname === "/auth/sms-start") {
      return authSmsStart(req, env);
    }
    if (url.pathname === "/auth/sms-verify") {
      return authSmsVerify(req, env);
    }
    if (url.pathname === "/advisor") {
      return advisorAsk(req, env);
    }
    return textbeltReply(req, env);
  },
};

// ---------------------------------------------------------------------------
// /auth/sms-start — phone whitelist check, mint OTP, send SMS, return bundle
// ---------------------------------------------------------------------------
//
// Body:    { "phone": "+16302479950" }
// Result:  { "ok": true, "nonce": "<hex>", "expires_at": "<iso>", "hmac": "<hex>" }
//          { "ok": false, "error": "..." }
//
// We reject unknown phones with the same generic error as a successful send
// so an attacker probing AUTH_PHONES can't enumerate which numbers are
// allowed. (We do log internally; production may want rate limiting.)
//
async function authSmsStart(req: Request, env: Env): Promise<Response> {
  let body: { phone?: string };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "Bad JSON" }, { status: 400 });
  }
  const phone = normalizePhone(body.phone || "");
  if (!phone) {
    return jsonResponse({ ok: false, error: "Missing or malformed phone" }, { status: 400 });
  }
  const allowed = parsePhoneList(env.AUTH_PHONES || "");
  if (allowed.length === 0) {
    return jsonResponse({ ok: false, error: "Server not configured" }, { status: 500 });
  }
  // Generic "ok" response on whitelist miss to avoid number enumeration.
  // We just don't actually send the SMS; the client will hit a verify failure.
  if (!allowed.includes(phone)) {
    console.log(`auth-sms: rejected phone ${phone}`);
    // Still return a fake-looking bundle so the client UI flow stays uniform.
    // Verify will reject because we won't sign anything real for this number.
    const fakeNonce     = randomHex(16);
    const fakeExpiresAt = new Date(Date.now() + AUTH_OTP_TTL_MS).toISOString();
    return jsonResponse({
      ok: true,
      nonce:      fakeNonce,
      expires_at: fakeExpiresAt,
      // HMAC bound to a phone we'll never accept — verify will fail.
      hmac:       await hmacHex(env.TEXTBELT_KEY, `auth-sms|${fakeNonce}|denied|${fakeExpiresAt}`),
    });
  }

  const code      = mintCode();
  const nonce     = randomHex(16);
  const expiresAt = new Date(Date.now() + AUTH_OTP_TTL_MS).toISOString();
  const hmac      = await hmacHex(
    env.TEXTBELT_KEY,
    `auth-sms|${code}|${phone}|${nonce}|${expiresAt}`,
  );

  const message = `Freis Farm: ${code} is your sign-in code. Expires in 5 min. Don't reply.`;
  const sent = await sendTextBelt(env, phone, message);
  if (!sent.ok) {
    console.error(`auth-sms textbelt send failed: ${sent.error}`);
    return jsonResponse({ ok: false, error: "SMS send failed" }, { status: 502 });
  }

  return jsonResponse({ ok: true, nonce, expires_at: expiresAt, hmac });
}

// ---------------------------------------------------------------------------
// /auth/sms-verify — verify OTP + HMAC, mint 30-day session token
// ---------------------------------------------------------------------------
//
// Body:    { "phone", "code", "nonce", "expires_at", "hmac" }
// Result:  { "ok": true, "token": "<hex>", "expires_at": "<iso>" }
//          { "ok": false, "error": "..." }
//
// On success we return the EXACT SAME token shape as /auth/login, so the
// client path after this point (storage, /auth/verify, expiry) is identical
// regardless of which login method was used.
//
async function authSmsVerify(req: Request, env: Env): Promise<Response> {
  let body: {
    phone?:      string;
    code?:       string;
    nonce?:      string;
    expires_at?: string;
    hmac?:       string;
  };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "Bad JSON" }, { status: 400 });
  }

  const phone     = normalizePhone(body.phone || "");
  const code      = (body.code       || "").trim();
  const nonce     = (body.nonce      || "").trim();
  const expiresAt = (body.expires_at || "").trim();
  const hmac      = (body.hmac       || "").trim();
  if (!phone || !code || !nonce || !expiresAt || !hmac) {
    return jsonResponse({ ok: false, error: "Missing field" }, { status: 400 });
  }

  // OTP expired?
  const t = Date.parse(expiresAt);
  if (!Number.isFinite(t) || t <= Date.now()) {
    return jsonResponse({ ok: false, error: "Code expired" }, { status: 401 });
  }

  // Phone still allowed?
  const allowed = parsePhoneList(env.AUTH_PHONES || "");
  if (!allowed.includes(phone)) {
    return jsonResponse({ ok: false, error: "Phone not allowed" }, { status: 403 });
  }

  // HMAC matches what we'd have signed in /auth/sms-start?
  const expected = await hmacHex(
    env.TEXTBELT_KEY,
    `auth-sms|${code}|${phone}|${nonce}|${expiresAt}`,
  );
  if (!timingSafeEqual(hmac, expected)) {
    return jsonResponse({ ok: false, error: "Wrong code" }, { status: 401 });
  }

  // Mint the 30-day session token — SAME shape as /auth/login output.
  const sessionExpiresAt = new Date(Date.now() + AUTH_TTL_MS).toISOString();
  const token            = await hmacHex(env.TEXTBELT_KEY, "auth|" + sessionExpiresAt);
  return jsonResponse({ ok: true, token, expires_at: sessionExpiresAt });
}

// ---------------------------------------------------------------------------
// /advisor — POST a question, get a Claude-generated reply
// ---------------------------------------------------------------------------
//
// Request body:
//   {
//     "question":   "May/June soy 11.95...",   // required
//     "channel":    "sms" | "web",             // default "web"
//     "token":      "<auth token from /auth/login or /auth/sms-verify>",
//     "expires_at": "<iso>",                   // companion to token
//     "history":    [ { "role":"user|assistant", "content":"..." } ]
//                                              // optional prior turns
//   }
//
// Response:
//   { "ok": true,  "reply": "...", "model": "claude-sonnet-4-6",
//     "tokens": { "in": N, "out": N }, "cost_usd": 0.0376 }
//   { "ok": false, "error": "..." }
//
// Auth: same HMAC-token shape as the dashboard. Anyone reaching this
// endpoint must already have logged in via SMS-OTP or the garage code.
// Unauthenticated calls get a 401, no advisor leakage.
//
// Model selection: question prefix-matched on /deep, /quick. Else
// Sonnet 4.6. Falls back to Haiku if Sonnet fails.
//
// Context: fetched from ADVISOR_CONTEXT_URL on every request, cached
// for 5 minutes inside the Worker via the Cache API. The context-builder
// publishes advisor_context.json to GitHub Pages once a day; this route
// just reads it.
//
// Persona: a fixed string baked in here so we never have to fetch it.
// Long, but that's the prompt — anything we change requires a deploy
// (intentional: prompt changes are policy changes).
//

const ADVISOR_PERSONA = `You are the Freis Farm trade advisor — a grain marketing co-pilot for Jimmy Meyer (Freis Farm, central Illinois, sells to Ritchie Grain via the Akron Services portal).

You are NOT a licensed financial or commodity advisor. You do not place trades. Your job is to help Jimmy think through marketing decisions using his own farm data.

## Ground rules

1. Answer using ONLY the farm context provided below. If a question requires data you don't have, say so plainly and ask Jimmy to paste it. Never invent bid prices, storage rates, or bushel positions.

2. Cite which farm fact you're leaning on — by sheet name or memory file ("per Storage State, you have 1,967 bu beans at Ritchie") — so Jimmy can verify. Do not paraphrase the data; state the number.

3. **Prefer structured fields over memory prose.** The context bundle has both narrative memory files AND computed values from the books (e.g. realized_storage_rates._working_rate_cents, the storage_state sheet, prices_history). When prose and a structured field describe the same fact, use the structured field — it's freshly computed from the ledger.

4. Show your math. If you compute a per-bushel storage charge, a basis spread, a breakeven, or a carry, write the arithmetic in one line.

5. Recommend, don't decide. Frame as "I'd consider X because Y" with the tradeoff. End SMS replies with one concrete next step (place a target order, call Akron, wait for X event).

6. Never claim certainty about future prices. Use ranges, scenarios, or "if/then." Do not say "the market will."

7. If Jimmy asks you to actually place an order, decline and instead draft the exact target/quantity/expiry for him to enter via the dashboard's existing OTP-confirmed order flow. The advisor is advice-only.

## Channel format

- sms: max 320 chars, plain text only (no markdown, no bullets, no emoji). One paragraph. End with one concrete next step. Use ¢ and $.
- web: full markdown allowed. Lead with the recommendation in 1 line, then the math, then the data citations, then a "Next step" line.

## Style

Direct, concrete, farmer-to-farmer. No filler. No "I'd be happy to." No safety preambles. If something is uncertain, say "uncertain" and why. If a number doesn't exist in the data you were given, say so.

## Glossary — Jimmy's shorthand

- MY 2024-2025 = "Marketing year," Sep 1 → Aug 31. The unit Jimmy thinks in.
- APP = Average Pricing Program. Ritchie's averaging product. Committed bushels for 2026: 1,500 corn + 500 soy. Don't double-sell these.
- Halo = the 1-week free storage window at Ritchie before charges kick in.
- Akron / Ritchie = same elevator. Akron Services is the portal.
- PC/FS = the alternate elevator (Posen Coop / FS Grain).
- Lattering = input supplier with recurring ~$10.5k bill.
- Quigley = Kevin Quigley, hay buyer.
- Milford = where fall calves go.
- GTC = Good-til-canceled order. Default order type Jimmy uses.
- Basis = cash bid minus nearby futures. Negative = "under."
- Carry = price difference between a nearer and farther delivery month.
- Net $/bu = from the books, already nets out commissions.

## Worked example — bean carry

Q (sms): "May and June soy bids both 11.95. Worth a target on June at 11.98 to cover storage on my 1,967 bu, or wait?"

Reasoning:
- Read realized_storage_rates._working_rate_cents → 3.21¢/bu/mo
- Read storage_state → 1,967 bu beans at Ritchie, 500 of them APP-committed
- 3¢ carry to 11.98 vs 3.21¢ storage → -0.21¢/bu (effectively breakeven)
- Bean sale plan from memory: ~1,000 bu 6/1 + ~1,000 bu 7/1
- Recommend $11.99-$12.00 GTC for the 6/1 leg

Good SMS reply: "Akron May/June soy both $11.95 — flat carry. Realized storage at Ritchie is $0.0321/bu/mo (per ledger), so $11.98 nets you about flat. Need $11.99 for a penny over storage, $12.00 cleaner. I'd target $12.00 GTC for the 6/1 leg (~1,000 bu). Reassess if it doesn't fill by mid-May."

Bad SMS reply: "Great question! There are several factors to consider..."  ← no math, no citation, no next step.
`;

interface AdvisorBody {
  question?:    string;
  channel?:     "sms" | "web";
  token?:       string;
  expires_at?:  string;
  history?:     Array<{ role: "user" | "assistant"; content: string }>;
}

interface AnthropicMessageResponse {
  content: Array<{ type: string; text?: string }>;
  usage:   { input_tokens: number; output_tokens: number };
  model:   string;
}

const ADVISOR_DEFAULT_MODEL = "claude-sonnet-4-6";
const ADVISOR_DEEP_MODEL    = "claude-opus-4-6";
const ADVISOR_QUICK_MODEL   = "claude-haiku-4-5-20251001";
// Rough per-million-token pricing for cost reporting.
const ADVISOR_PRICING: Record<string, { in: number; out: number }> = {
  "claude-sonnet-4-6":         { in: 3.0,  out: 15.0 },
  "claude-opus-4-6":           { in: 15.0, out: 75.0 },
  "claude-haiku-4-5-20251001": { in: 1.0,  out: 5.0  },
};

async function advisorAsk(req: Request, env: Env): Promise<Response> {
  let body: AdvisorBody;
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "Bad JSON" }, { status: 400 });
  }

  // 1. Auth — reuse the HMAC token issued by /auth/login or /auth/sms-verify.
  const token     = (body.token       || "").trim();
  const expiresAt = (body.expires_at  || "").trim();
  if (!token || !expiresAt) {
    return jsonResponse({ ok: false, error: "Missing auth" }, { status: 401 });
  }
  const t = Date.parse(expiresAt);
  if (!Number.isFinite(t) || t <= Date.now()) {
    return jsonResponse({ ok: false, error: "Auth expired" }, { status: 401 });
  }
  const expectedToken = await hmacHex(env.TEXTBELT_KEY, "auth|" + expiresAt);
  if (!timingSafeEqual(token, expectedToken)) {
    return jsonResponse({ ok: false, error: "Bad auth" }, { status: 401 });
  }

  // 2. Validate input.
  const rawQ = (body.question || "").trim();
  if (!rawQ) {
    return jsonResponse({ ok: false, error: "Empty question" }, { status: 400 });
  }
  if (rawQ.length > 1000) {
    return jsonResponse({ ok: false, error: "Question too long (>1000 chars)" }, { status: 400 });
  }
  const channel: "sms" | "web" = body.channel === "sms" ? "sms" : "web";

  // 3. Strip prompt-injection-flavored phrases from the user's text.
  // Light hygiene — the prompt is robust, but pre-strip the obvious.
  const cleanQ = rawQ
    .replace(/ignore (all |the )?(previous|prior|above) (instructions?|rules?|prompts?)/gi, "")
    .replace(/system prompt:?/gi, "")
    .trim();

  // 4. Detect /deep or /quick prefix → choose model.
  let model = ADVISOR_DEFAULT_MODEL;
  let userText = cleanQ;
  if (/^\s*\/deep\b/i.test(cleanQ)) {
    model = ADVISOR_DEEP_MODEL;
    userText = cleanQ.replace(/^\s*\/deep\b\s*/i, "").trim();
  } else if (/^\s*\/quick\b/i.test(cleanQ)) {
    model = ADVISOR_QUICK_MODEL;
    userText = cleanQ.replace(/^\s*\/quick\b\s*/i, "").trim();
  }
  if (!userText) {
    return jsonResponse({ ok: false, error: "Empty after prefix" }, { status: 400 });
  }

  // 5. Pull the context bundle (cached 5 min in the Worker's Cache API).
  let contextJson = "{}";
  try {
    contextJson = await fetchAdvisorContext(env);
  } catch (e) {
    console.error(`advisor: context fetch failed: ${(e as Error).message}`);
    // Still answer — the persona instructs the model to decline if data
    // is missing rather than make things up.
  }

  // 6. Assemble the system prompt.
  const channelTail = `\n\n## Active channel for this turn\n\n\`${channel}\`\n`;
  const systemPrompt =
    ADVISOR_PERSONA +
    "\n\n## Farm context bundle\n\n```json\n" +
    contextJson +
    "\n```\n" +
    channelTail;

  // 7. Build messages array (history + this turn).
  const history = Array.isArray(body.history) ? body.history.slice(-20) : [];
  const messages = [
    ...history
      .filter(h => (h.role === "user" || h.role === "assistant") && h.content)
      .map(h => ({ role: h.role, content: String(h.content).slice(0, 4000) })),
    { role: "user" as const, content: userText },
  ];

  // 8. Call Anthropic. One retry on transient failure, then fall back to Haiku.
  let resp: AnthropicMessageResponse | null = null;
  let usedModel = model;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      resp = await callAnthropic(env, usedModel, systemPrompt, messages);
      break;
    } catch (e) {
      console.error(`advisor: ${usedModel} attempt ${attempt} failed: ${(e as Error).message}`);
      if (attempt === 0 && usedModel !== ADVISOR_QUICK_MODEL) {
        usedModel = ADVISOR_QUICK_MODEL;  // graceful degrade
      }
    }
  }
  if (!resp) {
    return jsonResponse(
      { ok: false, error: "Advisor unreachable. Try again in a minute." },
      { status: 502 },
    );
  }

  const reply = resp.content
    .filter(b => b.type === "text" && typeof b.text === "string")
    .map(b => b.text as string)
    .join("");

  // 9. SMS hard-truncate guardrail. Persona aims for 320, but if a
  // model overflows we cap at 480 (≈3 SMS segments).
  let outText = reply;
  if (channel === "sms" && outText.length > 480) {
    outText = outText.slice(0, 477) + "...";
  }

  const inTok  = resp.usage.input_tokens;
  const outTok = resp.usage.output_tokens;
  const price  = ADVISOR_PRICING[usedModel] ?? { in: 0, out: 0 };
  const costUsd =
    (inTok  * price.in  / 1_000_000) +
    (outTok * price.out / 1_000_000);

  return jsonResponse({
    ok:       true,
    reply:    outText,
    model:    usedModel,
    channel,
    tokens:   { in: inTok, out: outTok },
    cost_usd: Number(costUsd.toFixed(4)),
  });
}

/**
 * Fetch advisor_context.json from GitHub Pages (or wherever
 * ADVISOR_CONTEXT_URL points). Cached in the Worker's Cache API for
 * 5 minutes so the same instance doesn't refetch on every turn.
 */
async function fetchAdvisorContext(env: Env): Promise<string> {
  const fallback = `https://jnimbles03.github.io/farm-trader/advisor/advisor_context.json`;
  const url = (env.ADVISOR_CONTEXT_URL || fallback).trim();
  const cacheKey = new Request(url + "?advisor-cache=v1");
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached.text();
  }
  const fresh = await fetch(url, {
    headers: { "User-Agent": "freis-farm-advisor-worker" },
  });
  if (!fresh.ok) {
    throw new Error(`context HTTP ${fresh.status}`);
  }
  const text = await fresh.text();
  // Cache 5 minutes. The bundle changes once a day at most.
  const cacheable = new Response(text, {
    headers: {
      "Content-Type":  "application/json",
      "Cache-Control": "public, max-age=300",
    },
  });
  await cache.put(cacheKey, cacheable);
  return text;
}

/**
 * Single Anthropic API call. Throws on non-2xx. 30s timeout — Workers
 * can wait that long, and Sonnet at 11k input tokens usually returns
 * in 3-8s.
 */
async function callAnthropic(
  env: Env, model: string, system: string,
  messages: Array<{ role: string; content: string }>,
): Promise<AnthropicMessageResponse> {
  if (!env.ANTHROPIC_API_KEY) {
    throw new Error("ANTHROPIC_API_KEY not configured");
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 30_000);
  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method:  "POST",
      headers: {
        "x-api-key":         env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
      },
      body: JSON.stringify({
        model,
        max_tokens: 1500,
        system,
        messages,
      }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const errBody = await r.text();
      throw new Error(`Anthropic ${r.status}: ${errBody.slice(0, 200)}`);
    }
    return await r.json() as AnthropicMessageResponse;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Phone helpers
// ---------------------------------------------------------------------------

function parsePhoneList(raw: string): string[] {
  return raw
    .split(",")
    .map(s => normalizePhone(s))
    .filter(s => s.length > 0);
}

function normalizePhone(raw: string): string {
  // Strip whitespace + hyphens + parens; require leading + and 10-15 digits.
  const cleaned = (raw || "").replace(/[\s\-()]/g, "");
  if (/^\+\d{10,15}$/.test(cleaned)) return cleaned;
  return "";
}

// ---------------------------------------------------------------------------
// /auth/login — exchange the garage code for a 30-day signed token
// ---------------------------------------------------------------------------
//
// Body:    { "code": "1234" }
// Result:  { "ok": true, "token": "<hex>", "expires_at": "<iso>" }   (200)
//          { "ok": false, "error": "..." }                            (4xx)
//
// Token is HMAC-SHA256(TEXTBELT_KEY, "auth|" + expires_at). It is bound
// only to the expiry instant — no user identity, no nonce — because the
// garage code itself is the only secret that proves "you are allowed in".
// Anyone holding a valid token is treated as authenticated until expiry.
//
async function authLogin(req: Request, env: Env): Promise<Response> {
  let body: { code?: string };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "Bad JSON" }, { status: 400 });
  }

  const code = (body.code || "").trim();
  if (!code) {
    return jsonResponse({ ok: false, error: "Missing code" }, { status: 400 });
  }

  const expected = (env.GARAGE_CODE || "").trim();
  if (!expected) {
    // Misconfigured server — fail closed, don't leak that the secret is missing.
    return jsonResponse({ ok: false, error: "Server not configured" }, { status: 500 });
  }
  if (!timingSafeEqual(code, expected)) {
    // Generic message — don't tell attackers whether they're close.
    return jsonResponse({ ok: false, error: "Wrong code" }, { status: 401 });
  }

  const expiresAt = new Date(Date.now() + AUTH_TTL_MS).toISOString();
  const token     = await hmacHex(env.TEXTBELT_KEY, "auth|" + expiresAt);
  return jsonResponse({ ok: true, token, expires_at: expiresAt });
}

// ---------------------------------------------------------------------------
// /auth/verify — re-check a previously-issued token
// ---------------------------------------------------------------------------
//
// Body:    { "token": "<hex>", "expires_at": "<iso>" }
// Result:  { "ok": true | false }   (always 200; client decides what to do)
//
// Pure HMAC check + expiry check. No DB, no state.
//
async function authVerify(req: Request, env: Env): Promise<Response> {
  let body: { token?: string; expires_at?: string };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false }, { status: 200 });
  }
  const token     = (body.token      || "").trim();
  const expiresAt = (body.expires_at || "").trim();
  if (!token || !expiresAt) {
    return jsonResponse({ ok: false });
  }
  // Expired?
  const t = Date.parse(expiresAt);
  if (!Number.isFinite(t) || t <= Date.now()) {
    return jsonResponse({ ok: false });
  }
  // HMAC matches?
  const expected = await hmacHex(env.TEXTBELT_KEY, "auth|" + expiresAt);
  if (!timingSafeEqual(token, expected)) {
    return jsonResponse({ ok: false });
  }
  return jsonResponse({ ok: true });
}

// ---------------------------------------------------------------------------
// /orders/start — mint OTP, send SMS, return signed bundle
// ---------------------------------------------------------------------------

async function ordersStart(req: Request, env: Env): Promise<Response> {
  let body: { phone?: string; payload?: unknown };
  try {
    body = await req.json();
  } catch {
    return textResponse("Bad JSON", { status: 400 });
  }
  const phone = (body.phone || "").trim();
  if (!ORDER_PHONE_WHITELIST.includes(phone)) {
    return textResponse("Phone not allowed", { status: 403 });
  }
  if (!body.payload || typeof body.payload !== "object") {
    return textResponse("Missing payload", { status: 400 });
  }

  const payloadJson = canonicalJson(body.payload);
  const payloadHash = await sha256Hex(payloadJson);
  const code        = mintCode();
  const nonce       = randomHex(16);
  const expiresAt   = new Date(Date.now() + OTP_TTL_MS).toISOString();
  const hmac        = await hmacHex(
    env.TEXTBELT_KEY,
    `${code}|${phone}|${payloadHash}|${nonce}|${expiresAt}`,
  );

  // Build a human-readable summary for the SMS — "1,500 bu corn limit @ $4.85
  // + 2 more". Drafts only, so we say so explicitly.
  const summary = describePayload(body.payload);
  const message = `Freis Farm: code ${code} confirms a draft order — ${summary}. Expires in 5 min. Code is for the dashboard, do NOT reply.`;

  const sent = await sendTextBelt(env, phone, message);
  if (!sent.ok) {
    console.error(`textbelt send failed: ${sent.error}`);
    return textResponse("SMS send failed", { status: 502 });
  }

  return jsonResponse({
    nonce,
    expires_at:   expiresAt,
    payload_hash: payloadHash,
    hmac,
  });
}

// ---------------------------------------------------------------------------
// /orders/submit — verify HMAC + code, fire repository_dispatch
// ---------------------------------------------------------------------------

interface SubmitBody {
  phone?:        string;
  payload?:      unknown;
  code?:         string;
  nonce?:        string;
  expires_at?:   string;
  payload_hash?: string;
  hmac?:         string;
}

async function ordersSubmit(req: Request, env: Env): Promise<Response> {
  let body: SubmitBody;
  try {
    body = await req.json();
  } catch {
    return textResponse("Bad JSON", { status: 400 });
  }
  const { phone, payload, code, nonce, expires_at, payload_hash, hmac } = body;
  if (!phone || !payload || !code || !nonce || !expires_at || !payload_hash || !hmac) {
    return textResponse("Missing fields", { status: 400 });
  }
  if (!ORDER_PHONE_WHITELIST.includes(phone)) {
    return textResponse("Phone not allowed", { status: 403 });
  }

  // Expiry check first — cheaper than the HMAC re-derivation.
  if (new Date(expires_at).getTime() < Date.now()) {
    return textResponse("Code expired", { status: 401 });
  }

  // Re-canonicalize payload and compare to the hash baked into the HMAC.
  // This catches any payload tampering between /start and /submit.
  const recomputedHash = await sha256Hex(canonicalJson(payload));
  if (!timingSafeEqual(recomputedHash, payload_hash)) {
    return textResponse("Payload changed since /start", { status: 401 });
  }

  // Re-derive HMAC with the user-supplied code; if it matches the bundle
  // returned by /start, the user must have read it from the SMS.
  const expectedHmac = await hmacHex(
    env.TEXTBELT_KEY,
    `${code}|${phone}|${payload_hash}|${nonce}|${expires_at}`,
  );
  if (!timingSafeEqual(expectedHmac, hmac)) {
    return textResponse("Code did not match", { status: 401 });
  }

  // Fire repository_dispatch. accept-order.yml picks this up.
  const dispatch = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method:  "POST",
      headers: {
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "freis-farm-orders-worker",
      },
      body: JSON.stringify({
        event_type: "order_draft",
        client_payload: {
          phone,
          payload,
          accepted_at: new Date().toISOString(),
          nonce,
        },
      }),
    },
  );

  if (!dispatch.ok) {
    const text = await dispatch.text();
    console.error(`github dispatch failed: ${dispatch.status} ${text}`);
    return textResponse(`GitHub dispatch failed: ${dispatch.status}`, { status: 502 });
  }

  return jsonResponse({ ok: true, status: "queued", nonce });
}

// ---------------------------------------------------------------------------
// /  — TextBelt inbound reply webhook (unchanged behavior)
// ---------------------------------------------------------------------------

async function textbeltReply(req: Request, env: Env): Promise<Response> {
  const rawBody = await req.text();

  const timestamp = req.headers.get("X-textbelt-timestamp");
  const signature = req.headers.get("X-textbelt-signature");
  if (!timestamp || !signature) {
    return new Response("Missing signature headers", { status: 401 });
  }
  const expected = await hmacHex(env.TEXTBELT_KEY, timestamp + rawBody);
  if (!timingSafeEqual(expected, signature)) {
    return new Response("Invalid signature", { status: 401 });
  }

  let payload: TextBeltReply;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return new Response("Bad JSON", { status: 400 });
  }
  const { fromNumber, text } = payload;
  if (!fromNumber || typeof text !== "string") {
    return new Response("Missing fromNumber or text", { status: 400 });
  }

  const dispatch = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "freis-farm-sms-reply-worker",
      },
      body: JSON.stringify({
        event_type: "sms_reply",
        client_payload: {
          phone:       fromNumber,
          text:        text,
          received_at: new Date().toISOString(),
          textbelt_id: payload.textId ?? null,
        },
      }),
    },
  );

  if (!dispatch.ok) {
    const body = await dispatch.text();
    console.error(`github dispatch failed: ${dispatch.status} ${body}`);
    return new Response(`GitHub dispatch failed: ${dispatch.status}`, { status: 502 });
  }

  console.log(`queued sms_reply for ${fromNumber}: ${text.slice(0, 40)}`);
  return new Response("OK\n", { status: 200 });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Stable JSON: recursively sort object keys so the same logical payload
// always serializes to the same bytes. Arrays preserve order (their
// position is meaningful — tranche 1 vs tranche 2).
function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function canonicalize(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(canonicalize);
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(value as Record<string, unknown>).sort()) {
    out[k] = canonicalize((value as Record<string, unknown>)[k]);
  }
  return out;
}

async function sha256Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const buf  = await crypto.subtle.digest("SHA-256", data);
  return bytesToHex(new Uint8Array(buf));
}

async function hmacHex(key: string, message: string): Promise<string> {
  const keyData  = new TextEncoder().encode(key);
  const msgData  = new TextEncoder().encode(message);
  const imported = await crypto.subtle.importKey(
    "raw", keyData, { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", imported, msgData);
  return bytesToHex(new Uint8Array(sig));
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Constant-time string compare — paranoid, but free.
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

function mintCode(): string {
  // 6 digits, leading zeros allowed. Cryptographic randomness — not strictly
  // required for SMS-OTP at this volume, but cheap so why not.
  const buf = new Uint8Array(4);
  crypto.getRandomValues(buf);
  const n = ((buf[0] << 24) | (buf[1] << 16) | (buf[2] << 8) | buf[3]) >>> 0;
  return String(n % 1_000_000).padStart(6, "0");
}

function randomHex(byteCount: number): string {
  const buf = new Uint8Array(byteCount);
  crypto.getRandomValues(buf);
  return bytesToHex(buf);
}

// Build a one-line summary of the order for the SMS body. Total bushels +
// the first tranche, with "(+ N more)" if there are extras.
function describePayload(payload: unknown): string {
  try {
    const p = payload as { crop?: string; tranches?: Array<{ bushels?: number; type?: string; limit_price?: number | null }> };
    const tranches = p.tranches || [];
    const totalBu  = tranches.reduce((a, t) => a + (Number(t.bushels) || 0), 0);
    const crop = (p.crop || "").toString();
    const cropLabel = crop === "soy" ? "soy" : (crop === "corn" ? "corn" : crop || "grain");
    const head = tranches[0];
    if (!head) return `${totalBu} bu ${cropLabel}`;
    const tail = tranches.length > 1 ? ` (+${tranches.length - 1} more)` : "";
    if (head.type === "market") {
      return `${totalBu} bu ${cropLabel} market${tail}`;
    }
    const px = typeof head.limit_price === "number" ? `$${head.limit_price.toFixed(2)}` : "limit";
    return `${totalBu} bu ${cropLabel} @ ${px}${tail}`;
  } catch {
    return "draft order";
  }
}

async function sendTextBelt(
  env: Env, phone: string, message: string,
): Promise<{ ok: true } | { ok: false; error: string }> {
  try {
    const r = await fetch("https://textbelt.com/text", {
      method:  "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        phone,
        message,
        key: env.TEXTBELT_KEY,
      }).toString(),
    });
    const data = await r.json() as { success?: boolean; error?: string };
    if (data.success) return { ok: true };
    return { ok: false, error: data.error || "TextBelt declined" };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}
