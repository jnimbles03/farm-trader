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
  TEXTBELT_KEY: string;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
  GARAGE_CODE:  string;
  AUTH_PHONES:  string;
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
